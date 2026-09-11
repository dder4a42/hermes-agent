"""Durable task board for long-horizon orchestration.

The board is intentionally small, JSON-backed, and inspectable. It gives the
main agent a stable DAG state outside the model context: nodes, dependencies,
status, reports, and verification summaries.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from utils import atomic_json_write


VALID_STATUSES = {
    "pending",
    "ready",
    "running",
    "blocked",
    "done",
    "failed",
    "timeout",
    "cancelled",
}

TERMINAL_STATUSES = {"done", "failed", "timeout", "cancelled"}

ALLOWED_TRANSITIONS = {
    "pending": {
        "pending",
        "ready",
        "running",
        "blocked",
        "done",
        "failed",
        "timeout",
        "cancelled",
    },
    "ready": {"ready", "running", "blocked", "cancelled"},
    "running": {"running", "done", "failed", "timeout", "blocked", "cancelled"},
    "blocked": {"blocked", "pending", "ready", "running", "cancelled"},
    "done": {"done"},
    "failed": {"failed", "pending", "ready", "cancelled"},
    "timeout": {"timeout", "pending", "ready", "cancelled"},
    "cancelled": {"cancelled"},
}

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_REPORT_PREVIEW_CHARS = 1200


# ---------------------------------------------------------------------------
# Report status vocabulary
# ---------------------------------------------------------------------------
# Children are asked for the SKILL.md report vocabulary
# (success|partial|failed|timeout); the board tracks lifecycle
# (pending|ready|running|blocked|done|failed|timeout|cancelled). These are two
# different vocabularies on purpose, and the attach path used to hand the
# report's own status straight to _normalize_status — so a parent following the
# documented protocol died on the first attach ("Invalid status: success").
# Translate instead of rejecting; unknown values still fail loudly.
REPORT_STATUS_ALIASES = {
    "success": "done",
    "succeeded": "done",
    "ok": "done",
    "complete": "done",
    "completed": "done",
    # "partial" is not done and not failed: map to the non-terminal status that
    # can be re-queued (blocked -> pending/ready/running are legal transitions),
    # so a follow-up run can pick the node back up without a manual reset.
    "partial": "blocked",
    "needs_followup": "blocked",
    "needs-followup": "blocked",
    "incomplete": "blocked",
    "error": "failed",
    "failure": "failed",
    "timed_out": "timeout",
    "canceled": "cancelled",
}

# Terminal statuses a node may only reach once its report has been verified —
# gated by ``longtask.require_verification_before_unlock`` (default off; see
# _require_verification).
_VERIFIED_TERMINAL_STATUSES = {"done"}


def normalize_report_status(value: Any) -> str:
    """Map a report/child status onto a board status.

    Accepts board statuses verbatim and translates the report aliases above.
    Raises ``LongtaskBoardError`` for anything else, naming both vocabularies so
    the model can self-correct in one turn.
    """
    status = str(value or "").strip().lower()
    if status in VALID_STATUSES:
        return status
    mapped = REPORT_STATUS_ALIASES.get(status)
    if mapped:
        return mapped
    raise LongtaskBoardError(
        f"Invalid status: {status}. Board statuses: {sorted(VALID_STATUSES)}; "
        f"report statuses: {sorted(REPORT_STATUS_ALIASES)}"
    )


def _require_verification() -> bool:
    """Read ``longtask.require_verification_before_unlock`` (default False).

    Off by default so existing boards keep their contract; when on, a node
    cannot reach a success-terminal status before ``longtask_verify_node`` has
    written a verdict. That is the invariant the skill states in prose ("do not
    unlock downstream work until the upstream report has a verification
    result") — this is the host-side enforcement of it.
    """
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        longtask_cfg = cfg.get("longtask") or {}
        if not isinstance(longtask_cfg, dict):
            return False
        return bool(longtask_cfg.get("require_verification_before_unlock", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Board locking
# ---------------------------------------------------------------------------
# Every mutator is load -> mutate -> save with no lock, and BOTH layers of
# concurrency are real: the runtime runs a turn's independent tool calls on
# worker threads (measured: 6 concurrent update_node(status="running") calls
# left 2 applied and 0 errors — silent lost updates), and two Hermes processes
# can share a workspace board. The RLock covers the in-process case; the POSIX
# flock covers the cross-process one. On Windows fcntl is absent and only the
# in-process lock applies.
try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

_PATH_LOCKS: Dict[str, "threading.RLock"] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _thread_lock(path: Path) -> "threading.RLock":
    key = str(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def board_lock(root: str | Path, session_id: str) -> Iterator[None]:
    """Serialize one board's read-modify-write cycle."""
    path = board_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(path):
        handle = None
        try:
            handle = open(f"{path}.lock", "a+")
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            if handle is not None:
                handle.close()
                handle = None
        try:
            yield
        finally:
            if handle is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()



class LongtaskBoardError(ValueError):
    """Raised for invalid board operations."""


def safe_id(value: str, *, fallback: str = "default") -> str:
    text = _SAFE_ID_RE.sub("_", str(value or "").strip())[:96].strip("._-")
    return text or fallback


def board_dir(root: str | Path, session_id: str) -> Path:
    return Path(root).expanduser().resolve() / safe_id(session_id)


def board_path(root: str | Path, session_id: str) -> Path:
    return board_dir(root, session_id) / "board.json"


def new_board(objective: str, nodes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    normalized_nodes = _normalize_nodes(nodes)
    board = {
        "task_id": f"task-{uuid.uuid4().hex[:12]}",
        "objective": str(objective or "").strip(),
        "created_at": _now(),
        "updated_at": _now(),
        "nodes": normalized_nodes,
        "global_state": {
            "decisions": [],
            "constraints": [],
            "open_questions": [],
            "next_ready": [],
        },
    }
    _refresh_ready_nodes(board)
    return board


def load_board(root: str | Path, session_id: str) -> Dict[str, Any]:
    path = board_path(root, session_id)
    if not path.exists():
        raise LongtaskBoardError(f"No longtask board exists for session {session_id!r}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LongtaskBoardError(f"Could not read longtask board: {exc}") from exc
    return _normalize_board(data)


def save_board(root: str | Path, session_id: str, board: Dict[str, Any]) -> Path:
    normalized = _normalize_board(board)
    normalized["updated_at"] = _now()
    path = board_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, normalized, indent=2, sort_keys=True)
    return path


def create_board(
    root: str | Path,
    session_id: str,
    objective: str,
    nodes: Iterable[Dict[str, Any]],
    *,
    overwrite: bool = False,
) -> Dict[str, Any]:
    with board_lock(root, session_id):
        return _create_board_locked(
            root, session_id, objective, nodes, overwrite=overwrite
        )


def _create_board_locked(
    root: str | Path,
    session_id: str,
    objective: str,
    nodes: Iterable[Dict[str, Any]],
    *,
    overwrite: bool = False,
) -> Dict[str, Any]:
    path = board_path(root, session_id)
    if path.exists() and not overwrite:
        raise LongtaskBoardError(
            f"Longtask board already exists for session {session_id!r}; "
            "pass overwrite=true to replace it"
        )
    board = new_board(objective, nodes)
    save_board(root, session_id, board)
    return board


def read_board(
    root: str | Path,
    session_id: str,
    *,
    node_id: Optional[str] = None,
) -> Dict[str, Any]:
    board = load_board(root, session_id)
    if node_id:
        node = _node_by_id(board, node_id)
        return {"task_id": board["task_id"], "objective": board["objective"], "node": node}
    return board


def next_ready_nodes(
    root: str | Path,
    session_id: str,
    *,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    # Also a writer: it records global_state.next_ready, so it needs the same
    # lock as the mutators or it can clobber a concurrent update.
    with board_lock(root, session_id):
        return _next_ready_nodes_locked(root, session_id, limit=limit)


def _next_ready_nodes_locked(
    root: str | Path,
    session_id: str,
    *,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    board = load_board(root, session_id)
    ready = compute_ready_nodes(board)
    if limit is not None and limit > 0:
        ready = ready[:limit]
    board["global_state"]["next_ready"] = [n["node_id"] for n in ready]
    save_board(root, session_id, board)
    return {
        "task_id": board["task_id"],
        "objective": board["objective"],
        "ready": ready,
        "blocked": [
            _public_node(n)
            for n in board["nodes"]
            if n["status"] in {"pending", "blocked"}
            and not _dependencies_done(board, n)
        ],
    }


def update_node(
    root: str | Path,
    session_id: str,
    node_id: str,
    *,
    status: Optional[str] = None,
    assigned_to: Optional[str] = None,
    claims: Optional[List[Dict[str, Any]]] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    verification: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    with board_lock(root, session_id):
        return _update_node_locked(
            root,
            session_id,
            node_id,
            status=status,
            assigned_to=assigned_to,
            claims=claims,
            evidence=evidence,
            verification=verification,
            notes=notes,
        )


def _update_node_locked(
    root: str | Path,
    session_id: str,
    node_id: str,
    *,
    status: Optional[str] = None,
    assigned_to: Optional[str] = None,
    claims: Optional[List[Dict[str, Any]]] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    verification: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    board = load_board(root, session_id)
    node = _node_by_id(board, node_id)
    if status is not None:
        new_status = _normalize_status(status)
        old_status = node["status"]
        if new_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
            raise LongtaskBoardError(
                f"Invalid node transition {node_id}: {old_status} -> {new_status}"
            )
        if new_status in {"ready", "running", "done"} and not _dependencies_done(board, node):
            raise LongtaskBoardError(
                f"Node {node_id} cannot become {new_status}; dependencies are not done"
            )
        if (
            new_status in _VERIFIED_TERMINAL_STATUSES
            and _require_verification()
            and not node.get("verification")
        ):
            raise LongtaskBoardError(
                f"Node {node_id} cannot become {new_status} before it has a "
                "verification result; call longtask_verify_node first "
                "(enable/disable with longtask.require_verification_before_unlock)"
            )
        node["status"] = new_status
    if assigned_to is not None:
        node["assigned_to"] = str(assigned_to).strip() or None
    if claims is not None:
        node["claims"] = _normalize_list_of_dicts(claims, "claims")
    if evidence is not None:
        node["evidence"] = _normalize_list_of_dicts(evidence, "evidence")
    if verification is not None:
        node["verification"] = _normalize_dict(verification, "verification")
    if notes is not None:
        node["notes"] = str(notes)
    node["updated_at"] = _now()
    _refresh_ready_nodes(board)
    save_board(root, session_id, board)
    return {"task_id": board["task_id"], "node": _public_node(node)}


def attach_report(
    root: str | Path,
    session_id: str,
    node_id: str,
    report: Dict[str, Any],
    *,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    with board_lock(root, session_id):
        return _attach_report_locked(
            root, session_id, node_id, report, status=status
        )


def _attach_report_locked(
    root: str | Path,
    session_id: str,
    node_id: str,
    report: Dict[str, Any],
    *,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    board = load_board(root, session_id)
    node = _node_by_id(board, node_id)
    report_data = _normalize_dict(report, "report")
    report_data.setdefault("recorded_at", _now())
    reports_dir = board_dir(root, session_id) / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{safe_id(node_id)}-{int(time.time())}.json"
    atomic_json_write(path, report_data, indent=2, sort_keys=True)

    rel_path = str(path.relative_to(board_dir(root, session_id)))
    node["report_path"] = rel_path
    node["report_preview"] = _preview(report_data)
    if isinstance(report_data.get("claims"), list):
        node["claims"] = _normalize_list_of_dicts(report_data["claims"], "claims")
    if isinstance(report_data.get("evidence"), list):
        node["evidence"] = _normalize_list_of_dicts(report_data["evidence"], "evidence")
    requested_status = status or report_data.get("status")
    if requested_status:
        # The child reports with the SKILL.md vocabulary; translate it onto the
        # board's lifecycle instead of rejecting it.
        new_status = normalize_report_status(requested_status)
        node["report_status"] = new_status
        if (
            new_status in _VERIFIED_TERMINAL_STATUSES
            and _require_verification()
            and not node.get("verification")
        ):
            # The report itself is persisted (re-attaching is idempotent), but
            # the terminal transition is refused: a child's self-report is a
            # claim, not a verdict.
            node["updated_at"] = _now()
            _refresh_ready_nodes(board)
            save_board(root, session_id, board)
            raise LongtaskBoardError(
                f"Node {node_id}: report recorded, but {new_status!r} needs a "
                "verification result first. Call longtask_verify_node, then "
                "longtask_update_node(status='done')."
            )
        if new_status not in ALLOWED_TRANSITIONS.get(node["status"], set()):
            raise LongtaskBoardError(
                f"Invalid node transition {node_id}: {node['status']} -> {new_status}"
            )
        node["status"] = new_status
    node["updated_at"] = _now()
    _refresh_ready_nodes(board)
    save_board(root, session_id, board)
    return {
        "task_id": board["task_id"],
        "node": _public_node(node),
        "report_path": str(path),
    }


def compute_ready_nodes(board: Dict[str, Any]) -> List[Dict[str, Any]]:
    normalized = _normalize_board(board)
    ready = []
    for node in _topological_nodes(normalized):
        if node["status"] in {"done", "running", "cancelled"}:
            continue
        if _dependencies_done(normalized, node):
            if node["status"] in {"pending", "ready", "failed", "timeout"}:
                ready.append(_public_node({**node, "status": "ready"}))
    return ready


def _normalize_board(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise LongtaskBoardError("Board must be a JSON object")
    board = copy.deepcopy(data)
    board["task_id"] = str(board.get("task_id") or f"task-{uuid.uuid4().hex[:12]}")
    board["objective"] = str(board.get("objective") or "").strip()
    board["created_at"] = str(board.get("created_at") or _now())
    board["updated_at"] = str(board.get("updated_at") or _now())
    board["nodes"] = _normalize_nodes(board.get("nodes") or [])
    gs = board.get("global_state")
    if not isinstance(gs, dict):
        gs = {}
    board["global_state"] = {
        "decisions": list(gs.get("decisions") or []),
        "constraints": list(gs.get("constraints") or []),
        "open_questions": list(gs.get("open_questions") or []),
        "next_ready": list(gs.get("next_ready") or []),
    }
    _validate_acyclic(board)
    _refresh_ready_nodes(board)
    return board


def _normalize_nodes(nodes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(nodes, list):
        nodes = list(nodes or [])
    out = []
    seen = set()
    for index, raw in enumerate(nodes, start=1):
        if not isinstance(raw, dict):
            raise LongtaskBoardError(f"Node {index} must be an object")
        node_id = safe_id(str(raw.get("node_id") or raw.get("id") or f"N{index}"))
        if node_id in seen:
            raise LongtaskBoardError(f"Duplicate node_id: {node_id}")
        seen.add(node_id)
        deps = [safe_id(str(dep)) for dep in raw.get("dependencies", raw.get("deps", [])) or []]
        node = {
            "node_id": node_id,
            "goal": str(raw.get("goal") or "").strip(),
            "dependencies": deps,
            "status": _normalize_status(raw.get("status") or "pending"),
            "assigned_to": raw.get("assigned_to"),
            "claims": _normalize_list_of_dicts(raw.get("claims") or [], "claims"),
            "evidence": _normalize_list_of_dicts(raw.get("evidence") or [], "evidence"),
            "report_path": raw.get("report_path"),
            "report_status": raw.get("report_status"),
            "verification": raw.get("verification"),
            "created_at": str(raw.get("created_at") or _now()),
            "updated_at": str(raw.get("updated_at") or _now()),
        }
        if raw.get("report_preview") is not None:
            node["report_preview"] = str(raw.get("report_preview"))
        if raw.get("notes") is not None:
            node["notes"] = str(raw.get("notes"))
        if not node["goal"]:
            raise LongtaskBoardError(f"Node {node_id} must have a goal")
        out.append(node)
    node_ids = {n["node_id"] for n in out}
    for node in out:
        missing = [dep for dep in node["dependencies"] if dep not in node_ids]
        if missing:
            raise LongtaskBoardError(
                f"Node {node['node_id']} depends on unknown node(s): {', '.join(missing)}"
            )
    return out


def _normalize_status(value: Any) -> str:
    status = str(value or "pending").strip().lower()
    if status not in VALID_STATUSES:
        raise LongtaskBoardError(f"Invalid status: {status}")
    return status


def _normalize_list_of_dicts(value: Any, name: str) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise LongtaskBoardError(f"{name} must be a list")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise LongtaskBoardError(f"{name} items must be objects")
        out.append(copy.deepcopy(item))
    return out


def _normalize_dict(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise LongtaskBoardError(f"{name} must be an object")
    return copy.deepcopy(value)


def _node_by_id(board: Dict[str, Any], node_id: str) -> Dict[str, Any]:
    wanted = safe_id(node_id)
    for node in board["nodes"]:
        if node["node_id"] == wanted:
            return node
    raise LongtaskBoardError(f"Unknown node_id: {node_id}")


def _dependencies_done(board: Dict[str, Any], node: Dict[str, Any]) -> bool:
    nodes = {n["node_id"]: n for n in board["nodes"]}
    return all(nodes[dep]["status"] == "done" for dep in node.get("dependencies", []))


def _refresh_ready_nodes(board: Dict[str, Any]) -> None:
    board.setdefault("global_state", {})
    ready_ids = [node["node_id"] for node in compute_ready_nodes_no_normalize(board)]
    board["global_state"]["next_ready"] = ready_ids


def compute_ready_nodes_no_normalize(board: Dict[str, Any]) -> List[Dict[str, Any]]:
    ready = []
    for node in _topological_nodes_no_validate(board):
        if node["status"] in {"done", "running", "cancelled", "blocked"}:
            continue
        if _dependencies_done(board, node):
            ready.append(_public_node({**node, "status": "ready"}))
    return ready


def _topological_nodes(board: Dict[str, Any]) -> List[Dict[str, Any]]:
    _validate_acyclic(board)
    return _topological_nodes_no_validate(board)


def _topological_nodes_no_validate(board: Dict[str, Any]) -> List[Dict[str, Any]]:
    nodes = {n["node_id"]: n for n in board["nodes"]}
    ordered = []
    temporary = set()
    permanent = set()

    def visit(node_id: str) -> None:
        if node_id in permanent:
            return
        if node_id in temporary:
            raise LongtaskBoardError("Task graph contains a dependency cycle")
        temporary.add(node_id)
        for dep in nodes[node_id].get("dependencies", []):
            visit(dep)
        temporary.remove(node_id)
        permanent.add(node_id)
        ordered.append(nodes[node_id])

    for node_id in nodes:
        visit(node_id)
    return ordered


def _validate_acyclic(board: Dict[str, Any]) -> None:
    _topological_nodes_no_validate(board)


def _public_node(node: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "node_id",
        "goal",
        "dependencies",
        "status",
        "assigned_to",
        "claims",
        "evidence",
        "report_path",
        "report_preview",
        "report_status",
        "verification",
        "notes",
    )
    return {k: copy.deepcopy(node[k]) for k in keys if k in node and node[k] is not None}


def _preview(data: Dict[str, Any]) -> str:
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if len(text) <= _REPORT_PREVIEW_CHARS:
        return text
    return text[:_REPORT_PREVIEW_CHARS] + "...[truncated]"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
