"""Durable task board for long-horizon orchestration.

The board is intentionally small, JSON-backed, and inspectable. It gives the
main agent a stable DAG state outside the model context: items, dependencies,
two separate state axes (runtime-owned ``execution`` and agent-owned
``resolution``), attached reports, and verification summaries.
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


# ---------------------------------------------------------------------------
# Two axes: execution (runtime-owned) and resolution (agent-owned)
# ---------------------------------------------------------------------------
# Split deliberately, per AgentOS: "The coordinator owns these semantic fields,
# while the runtime owns the status of each dispatched execution. This separation
# prevents a Task Board update from overwriting the state of a live asynchronous
# job" (arXiv 2608.23283 §3.3.2). A single conflated `status` could not express
# the difference between "a child said success" and "the result is back AND
# sufficiently checked, so the item is closed" — and it let a report arriving
# from a child advance the plan by itself.
#
#   execution  — facts about a dispatched run. Written by the HOST only
#                (never exposed on the tool schema).
#   resolution — the coordinator's semantic judgement. Written by the AGENT.
#                `resolved` means "the requested result has been returned and
#                sufficiently checked", not "a process exited" (§3.3.2).
VALID_EXECUTION = {
    "none",
    "queued",
    "running",
    "reported",
    "failed",
    "timeout",
    "cancelled",
}

VALID_RESOLUTION = {"open", "in_progress", "resolved", "cancelled"}

ALLOWED_RESOLUTION_TRANSITIONS = {
    "open": {"open", "in_progress", "resolved", "cancelled"},
    "in_progress": {"in_progress", "open", "resolved", "cancelled"},
    # Reopening is deliberate: a superseded premise must be able to invalidate
    # its descendants, and that starts by reopening the item it rested on.
    "resolved": {"resolved", "open"},
    "cancelled": {"cancelled"},
}

# Legacy single-axis statuses. Migrated on load and accepted on write, so boards
# written before the split (and models still saying "done") keep working.
_LEGACY_STATUS_MIGRATION = {
    "pending": ("none", "open"),
    "ready": ("none", "open"),
    "running": ("running", "in_progress"),
    "blocked": ("none", "open"),
    "done": ("reported", "resolved"),
    "failed": ("failed", "open"),
    "timeout": ("timeout", "open"),
    "cancelled": ("cancelled", "cancelled"),
}
LEGACY_STATUSES = frozenset(_LEGACY_STATUS_MIGRATION)

# A child's own words about its result. ADVISORY ONLY: it is recorded for the
# reader and never advances `resolution` — that is what a verdict plus an
# explicit `resolved` are for. Synonyms are normalised so the same outcome in
# different words compares equal; anything unrecognised is kept verbatim rather
# than rejected, because a report label is data, not a state transition.
REPORT_STATUS_ALIASES = {
    "success": "success",
    "succeeded": "success",
    "ok": "success",
    "complete": "success",
    "completed": "success",
    "done": "success",
    "partial": "partial",
    "needs_followup": "partial",
    "needs-followup": "partial",
    "incomplete": "partial",
    "error": "failed",
    "failure": "failed",
    "failed": "failed",
    "timed_out": "timeout",
    "timeout": "timeout",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}
VALID_REPORT_STATUSES = frozenset(
    {"success", "partial", "failed", "timeout", "cancelled", "unknown"}
)


def normalize_execution(value: Any) -> str:
    execution = str(value or "none").strip().lower()
    if execution not in VALID_EXECUTION:
        raise LongtaskBoardError(
            f"Invalid execution: {execution}. Expected one of {sorted(VALID_EXECUTION)}"
        )
    return execution


def normalize_resolution(value: Any) -> str:
    resolution = str(value or "open").strip().lower()
    if resolution in VALID_RESOLUTION:
        return resolution
    legacy = _LEGACY_STATUS_MIGRATION.get(resolution)
    if legacy:
        return legacy[1]
    raise LongtaskBoardError(
        f"Invalid resolution: {resolution}. Expected one of {sorted(VALID_RESOLUTION)}"
    )


def normalize_report_status(value: Any) -> str:
    """Normalise a child's self-reported status. Advisory; never a board state."""
    status = str(value or "").strip().lower()
    if not status:
        return "unknown"
    if status in VALID_REPORT_STATUSES:
        return status
    mapped = REPORT_STATUS_ALIASES.get(status)
    if mapped:
        return mapped
    return status[:40]


_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_REPORT_PREVIEW_CHARS = 1200


def _resolution_blocked_by_gate(node: Dict[str, Any], new_resolution: str) -> Optional[str]:
    """Reason a resolution transition is refused, or ``None`` when allowed.

    Only ``resolved`` is gated, and only when
    ``longtask.require_verification_before_unlock`` is on: `resolved` claims the
    result was returned AND sufficiently checked, so a recorded verdict is the
    evidence for that claim — a missing or non-accepted verdict is a refusal,
    not a warning.
    """
    if new_resolution != "resolved":
        return None
    if not _require_verification():
        return None
    verification = node.get("verification")
    if not isinstance(verification, dict):
        return (
            "no verification result yet; run longtask_verify_node first "
            "(longtask.require_verification_before_unlock)"
        )
    verdict = str(verification.get("verdict") or "").strip().lower()
    if verdict != "accepted":
        return (
            f"verification verdict is {verdict or 'missing'!r}, not 'accepted' — "
            "resolve the contested claims first"
        )
    return None



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
        "blocked": _blocked_nodes(board, ready),
    }


def _blocked_nodes(
    board: Dict[str, Any], ready: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Open items that are not ready, each with the reason it is waiting.

    The reason matters more than the label: a model that sees only "blocked" will
    guess, whereas "blocked_by: [N2]" tells it exactly which upstream item to
    resolve (or which external decision to chase).
    """
    ready_ids = {node["node_id"] for node in ready}
    blocked = []
    for node in board["nodes"]:
        if node["resolution"] != "open" or node["node_id"] in ready_ids:
            continue
        entry = _public_node(node)
        waiting = _unresolved_dependencies(board, node)
        if waiting:
            entry["blocked_by"] = waiting
            # A cancelled dependency can never become resolved, so name it
            # separately: the item needs REWIRING or cancelling, not waiting.
            cancelled = [
                dep for dep in waiting if _resolution_of(board, dep) == "cancelled"
            ]
            if cancelled:
                entry["blocked_by_cancelled"] = cancelled
        elif node.get("blocked_reason"):
            entry["blocked_by"] = [node["blocked_reason"]]
        blocked.append(entry)
    return blocked


def _resolution_of(board: Dict[str, Any], node_id: str) -> str:
    for node in board["nodes"]:
        if node["node_id"] == node_id:
            return str(node.get("resolution") or "open")
    return "unknown"


def update_node(
    root: str | Path,
    session_id: str,
    node_id: str,
    *,
    resolution: Optional[str] = None,
    execution: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    goal: Optional[str] = None,
    dependencies: Optional[List[str]] = None,
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
            resolution=resolution,
            execution=execution,
            blocked_reason=blocked_reason,
            goal=goal,
            dependencies=dependencies,
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
    resolution: Optional[str] = None,
    execution: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    goal: Optional[str] = None,
    dependencies: Optional[List[str]] = None,
    assigned_to: Optional[str] = None,
    claims: Optional[List[Dict[str, Any]]] = None,
    evidence: Optional[List[Dict[str, Any]]] = None,
    verification: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    board = load_board(root, session_id)
    node = _node_by_id(board, node_id)
    if resolution is not None:
        new_resolution = normalize_resolution(resolution)
        old_resolution = node["resolution"]
        if new_resolution not in ALLOWED_RESOLUTION_TRANSITIONS.get(old_resolution, set()):
            raise LongtaskBoardError(
                f"Invalid resolution transition {node_id}: "
                f"{old_resolution} -> {new_resolution}"
            )
        if new_resolution == "resolved" and not _dependencies_resolved(board, node):
            raise LongtaskBoardError(
                f"Node {node_id} cannot be resolved; dependencies are not resolved"
            )
        gate_reason = _resolution_blocked_by_gate(node, new_resolution)
        if gate_reason:
            raise LongtaskBoardError(
                f"Node {node_id} cannot be resolved: {gate_reason}"
            )
        node["resolution"] = new_resolution
    if execution is not None:
        # Runtime-owned axis. Exposed on the Python API for the host (and for
        # tests), deliberately NOT on the tool schema: a model must not declare
        # the status of a dispatched execution.
        node["execution"] = normalize_execution(execution)
    if blocked_reason is not None:
        node["blocked_reason"] = str(blocked_reason).strip() or None
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
    if goal is not None:
        # Revising an item's description is how a plan revision is expressed;
        # AgentOS: "plan revisions are expressed as tool-mediated edits to it".
        text = str(goal).strip()
        if not text:
            raise LongtaskBoardError(f"Node {node_id} must keep a non-empty goal")
        node["goal"] = text
    if dependencies is not None:
        new_deps = [safe_id(str(dep)) for dep in dependencies]
        if node_id in new_deps:
            raise LongtaskBoardError(f"Node {node_id} cannot depend on itself")
        known = {other["node_id"] for other in board["nodes"]}
        missing = [dep for dep in new_deps if dep not in known]
        if missing:
            raise LongtaskBoardError(
                f"Node {node_id} cannot depend on unknown node(s): {', '.join(missing)}"
            )
        node["dependencies"] = new_deps
        # Rewiring is the one edit that can introduce a cycle; validate before
        # persisting so a bad edit is rejected whole.
        _validate_acyclic(board)
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
    report_status: Optional[str] = None,
) -> Dict[str, Any]:
    with board_lock(root, session_id):
        return _attach_report_locked(
            root, session_id, node_id, report, report_status=report_status
        )


def _attach_report_locked(
    root: str | Path,
    session_id: str,
    node_id: str,
    report: Dict[str, Any],
    *,
    report_status: Optional[str] = None,
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
    # The child's own label is recorded for the reader and NEVER advances
    # `resolution`. AgentOS: "A subagent execution may be reported while its item
    # remains open because the report is incomplete, contradicted, or awaiting
    # verification" (§3.3.2). Resolving is a separate, explicit act with its own
    # evidence (a verdict).
    node["report_status"] = normalize_report_status(
        report_status if report_status is not None else report_data.get("status")
    )
    node["execution"] = "reported"
    node["updated_at"] = _now()
    _refresh_ready_nodes(board)
    save_board(root, session_id, board)
    return {
        "task_id": board["task_id"],
        "node": _public_node(node),
        "report_path": str(path),
    }


def add_nodes(
    root: str | Path,
    session_id: str,
    nodes: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Append items to an existing board.

    The DAG is not frozen at create time: AgentOS keeps the board mutable while
    the run proceeds "so new subquestions can be registered as evidence changes
    the plan" (§3.3.2). Validation matches create time — duplicate ids, unknown
    dependencies and cycles are rejected — so an appended item may depend on
    existing work without being able to corrupt the graph.
    """
    with board_lock(root, session_id):
        return _add_nodes_locked(root, session_id, nodes)


def _add_nodes_locked(
    root: str | Path,
    session_id: str,
    nodes: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    board = load_board(root, session_id)
    existing = {node["node_id"] for node in board["nodes"]}
    incoming = _normalize_nodes(nodes, known_ids=existing)
    if not incoming:
        raise LongtaskBoardError("add_nodes requires at least one item")
    clash = sorted({node["node_id"] for node in incoming} & existing)
    if clash:
        raise LongtaskBoardError(
            f"node_id already exists on this board: {', '.join(clash)}"
        )
    board["nodes"].extend(incoming)
    _validate_acyclic(board)
    _refresh_ready_nodes(board)
    save_board(root, session_id, board)
    return {
        "task_id": board["task_id"],
        "added": [node["node_id"] for node in incoming],
        "node_count": len(board["nodes"]),
        "next_ready": board["global_state"]["next_ready"],
    }


def cancel_node(
    root: str | Path,
    session_id: str,
    node_id: str,
    *,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Cancel an item, reporting — never silently cascading to — its dependents.

    A cancelled item is terminal on the resolution axis. Its dependents are NOT
    cancelled for you: they stay open and now report the cancelled dependency in
    ``blocked_by``, which is the signal to either rewire them (``update_node``
    with new ``dependencies``) or cancel them too. Cascading silently would hide
    the decision from the coordinator.
    """
    with board_lock(root, session_id):
        return _cancel_node_locked(root, session_id, node_id, reason=reason)


def _cancel_node_locked(
    root: str | Path,
    session_id: str,
    node_id: str,
    *,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    board = load_board(root, session_id)
    node = _node_by_id(board, node_id)
    node["resolution"] = "cancelled"
    if reason:
        existing_notes = str(node.get("notes") or "").strip()
        node["notes"] = (
            f"{existing_notes}\ncancelled: {reason}".strip() if existing_notes
            else f"cancelled: {reason}"
        )
    dependents = [
        other["node_id"]
        for other in board["nodes"]
        if node_id in (other.get("dependencies") or [])
        and other["resolution"] in {"open", "in_progress"}
    ]
    node["updated_at"] = _now()
    _refresh_ready_nodes(board)
    save_board(root, session_id, board)
    return {
        "task_id": board["task_id"],
        "node": _public_node(node),
        "dependents_to_review": dependents,
    }


def compute_ready_nodes(board: Dict[str, Any]) -> List[Dict[str, Any]]:
    normalized = _normalize_board(board)
    return [
        _public_node({**node, "ready": True})
        for node in _topological_nodes(normalized)
        if _is_ready(normalized, node)
    ]


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


def _normalize_nodes(
    nodes: Iterable[Dict[str, Any]],
    known_ids: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Normalise item dicts, validating ids and dependencies.

    ``known_ids`` are ids that already exist elsewhere on the board (used when
    appending): a dependency may point at them even though they are not part of
    this batch.
    """
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
        execution, resolution = _resolve_axes(raw)
        blocked_reason = raw.get("blocked_reason")
        node = {
            "node_id": node_id,
            "goal": str(raw.get("goal") or "").strip(),
            "dependencies": deps,
            "execution": execution,
            "resolution": resolution,
            "blocked_reason": (str(blocked_reason).strip() or None) if blocked_reason else None,
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
    legal_ids = node_ids | {safe_id(str(extra)) for extra in (known_ids or ())}
    for node in out:
        missing = [dep for dep in node["dependencies"] if dep not in legal_ids]
        if missing:
            raise LongtaskBoardError(
                f"Node {node['node_id']} depends on unknown node(s): {', '.join(missing)}"
            )
    return out


def _resolve_axes(raw: Dict[str, Any]) -> tuple[str, str]:
    """Return ``(execution, resolution)`` for a raw node.

    Boards written before the split carry a single ``status``; migrate it so an
    on-disk board keeps loading instead of needing a rewrite. Both new keys win
    when present, so a partially migrated node is read as written.
    """
    has_execution = raw.get("execution") is not None
    has_resolution = raw.get("resolution") is not None
    if has_execution or has_resolution:
        execution = (
            normalize_execution(raw.get("execution")) if has_execution else "none"
        )
        resolution = (
            normalize_resolution(raw.get("resolution")) if has_resolution else "open"
        )
        return execution, resolution
    legacy = str(raw.get("status") or "pending").strip().lower()
    migrated = _LEGACY_STATUS_MIGRATION.get(legacy)
    if migrated is None:
        raise LongtaskBoardError(
            f"Invalid status: {legacy}. Expected a legacy status "
            f"{sorted(LEGACY_STATUSES)} or execution/resolution"
        )
    execution, resolution = migrated
    # A legacy `done` only becomes `reported` if a report actually exists;
    # otherwise the item was closed without one.
    if execution == "reported" and not raw.get("report_path"):
        execution = "none"
    return execution, resolution


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


def _unresolved_dependencies(board: Dict[str, Any], node: Dict[str, Any]) -> List[str]:
    nodes = {n["node_id"]: n for n in board["nodes"]}
    return [
        dep for dep in node.get("dependencies", []) if nodes[dep]["resolution"] != "resolved"
    ]


def _dependencies_resolved(board: Dict[str, Any], node: Dict[str, Any]) -> bool:
    return not _unresolved_dependencies(board, node)


def _is_ready(board: Dict[str, Any], node: Dict[str, Any]) -> bool:
    """True when an item may be dispatched now.

    Readiness is DERIVED, never stored: an item is ready while it is ``open``, is
    not parked on an external decision, and every dependency is ``resolved``.
    This replaces the old ``status == "ready"`` fiction, where a stored state had
    to be kept in sync with the DAG by hand.
    """
    if node["resolution"] != "open":
        return False
    if node.get("blocked_reason"):
        return False
    return _dependencies_resolved(board, node)


def _refresh_ready_nodes(board: Dict[str, Any]) -> None:
    board.setdefault("global_state", {})
    board["global_state"]["next_ready"] = [
        node["node_id"]
        for node in _topological_nodes_no_validate(board)
        if _is_ready(board, node)
    ]


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
        "execution",
        "resolution",
        "blocked_reason",
        "ready",
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


def render_board_summary(
    board: Dict[str, Any],
    *,
    max_items: int = 8,
    include_resolved: bool = True,
) -> str:
    """Plain-text rendering of a board.

    One renderer for every consumer: the `/board` slash command today, and the
    event-driven re-injection planned for P4 — so the CLI, the agent, and the
    compression handoff can never disagree about what the board says. Readiness
    is derived with the board's own rule, never read from a stored field.
    """
    normalized = _normalize_board(board)
    nodes = normalized["nodes"]
    resolution_counts: Dict[str, int] = {}
    execution_counts: Dict[str, int] = {}
    for node in nodes:
        resolution_counts[node["resolution"]] = (
            resolution_counts.get(node["resolution"], 0) + 1
        )
        execution_counts[node["execution"]] = execution_counts.get(node["execution"], 0) + 1

    lines = [
        f"board {normalized['task_id']} — {normalized['objective']}",
        "  resolution: "
        + " ".join(f"{key}={resolution_counts[key]}" for key in sorted(resolution_counts))
        + "    execution: "
        + " ".join(f"{key}={execution_counts[key]}" for key in sorted(execution_counts)),
    ]

    def _item_line(node: Dict[str, Any], extra: str = "") -> str:
        deps = ",".join(node.get("dependencies") or []) or "-"
        # No square brackets: this text is printed through Rich (and later
        # injected into prompts), and [...] would be parsed as markup.
        line = f"    {node['node_id']:<10} deps={deps}  {node['goal'][:80]}"
        return f"{line}  {extra}".rstrip()

    def _section(
        title: str,
        items: List[Dict[str, Any]],
        *,
        extra_for: Optional[Any] = None,
    ) -> None:
        if not items:
            return
        lines.append(f"  {title} ({len(items)}):")
        for node in items[:max_items]:
            lines.append(_item_line(node, extra_for(node) if extra_for else ""))
        if len(items) > max_items:
            lines.append(f"    ... {len(items) - max_items} more")

    _section("ready frontier", [n for n in nodes if _is_ready(normalized, n)])
    _section(
        "in progress", [n for n in nodes if n["resolution"] == "in_progress"]
    )
    _section(
        "waiting on a decision",
        [n for n in nodes if n["resolution"] == "open" and n.get("blocked_reason")],
    )
    _section(
        "blocked",
        [
            n
            for n in nodes
            if n["resolution"] == "open"
            and not _is_ready(normalized, n)
            and not n.get("blocked_reason")
        ],
        extra_for=lambda n: "blocked_by: "
        + ", ".join(_unresolved_dependencies(normalized, n)),
    )
    if include_resolved:
        _section(
            "resolved",
            [n for n in nodes if n["resolution"] == "resolved"],
            extra_for=lambda n: (
                f"verdict={str((n.get('verification') or {}).get('verdict') or 'none')}"
            ),
        )
    _section("cancelled", [n for n in nodes if n["resolution"] == "cancelled"])
    return "\n".join(lines)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
