#!/usr/bin/env python3
"""Long-horizon task board tools.

These tools keep DAG state outside the model context so the main agent can
schedule subagents by dependency order and survive context compression.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from agent.longtask_board import (
    LongtaskBoardError,
    add_nodes,
    attach_report,
    board_dir,
    cancel_node,
    create_board,
    load_board,
    next_ready_nodes,
    read_board,
    update_node,
)
from agent.longtask_verifier import verify_report_with_llm
from tools.registry import registry, tool_error


def _workspace_root(parent_agent: Any = None) -> Path:
    """Resolve the workspace the board lives under.

    ``TERMINAL_CWD`` is probed FIRST because it is the one input both sides can
    see: the gateway bridges ``terminal.cwd`` into it, and the context engine
    that renders the board into a compression handoff has no agent object to
    consult (plugins/context_engine/long_horizon/__init__.py::_task_board_roots).
    The probe order has to agree with that engine or the board silently drops out
    of the handoff. The agent's own hints follow, then the process cwd.
    """
    candidates = [os.environ.get("TERMINAL_CWD")]
    if parent_agent is not None:
        candidates.extend(
            [
                getattr(parent_agent, "terminal_cwd", None),
                getattr(parent_agent, "cwd", None),
            ]
        )
        hints = getattr(parent_agent, "_subdirectory_hints", None)
        candidates.append(getattr(hints, "working_dir", None))
    for value in candidates:
        if value:
            return Path(str(value)).expanduser().resolve()
    return Path.cwd().resolve()


def _session_id(
    parent_agent: Any = None,
    explicit: Optional[str] = None,
    runtime_session_id: Optional[str] = None,
) -> str:
    """Resolve which board a call addresses.

    The runtime dispatch passes ``session_id`` but NEVER ``parent_agent`` —
    ``model_tools.handle_function_call`` calls ``registry.dispatch(..., task_id=,
    session_id=, user_task=)`` and only the plugin path injects a parent agent
    (hermes_cli/plugins.py). Keying the board off ``parent_agent`` alone therefore
    fell back to "default": every session in a workspace shared ONE board, while
    the compression engine looked up the real session id and never found it.

    Precedence: an explicit tool argument (a deliberate override) > the runtime
    session id > the parent agent > "default".
    """
    if explicit:
        return str(explicit)
    if runtime_session_id:
        return str(runtime_session_id)
    if parent_agent is not None:
        sid = getattr(parent_agent, "session_id", None)
        if sid:
            return str(sid)
    return "default"


def _root(parent_agent: Any = None, explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return _workspace_root(parent_agent) / ".hermes" / "tasks"


def _ok(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _handle_error(exc: Exception) -> str:
    if isinstance(exc, LongtaskBoardError):
        return tool_error(str(exc))
    return tool_error(f"longtask tool failed: {exc}")


def _child_board_refusal() -> Optional[str]:
    """Refuse board access from a delegate_task child.

    The board is the parent agent's planning state, and a child could not act on
    this board even if it tried: the board is keyed by the acting agent's
    session_id, and a child gets its own generated id, so the call would resolve
    a different directory (error, or a silently divergent shadow board). This is
    the runtime half of ``DELEGATE_BLOCKED_TOOLS`` in tools/delegate_tool.py —
    children normally never receive these schemas, and a future leak through a
    composite toolset must not become board corruption.
    """
    try:
        from agent.delegation_context import is_delegated_child_context

        if is_delegated_child_context():
            return tool_error(
                "Refused: the long-horizon board belongs to the parent agent. "
                "A delegate_task child executes one bounded node and returns its "
                "findings as a claim-evidence report; the parent attaches and "
                "verifies it."
            )
    except Exception:
        return None
    return None


def _board_handler(handler):
    """Wrap a longtask handler with the delegated-child refusal."""

    def wrapper(args: Dict[str, Any], **kw) -> str:
        refusal = _child_board_refusal()
        if refusal:
            return refusal
        return handler(args, **kw)

    wrapper.__name__ = getattr(handler, "__name__", "longtask_handler")
    wrapper.__doc__ = getattr(handler, "__doc__", None)
    return wrapper


def _load_longtask_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        longtask = cfg.get("longtask") or {}
        return longtask if isinstance(longtask, dict) else {}
    except Exception:
        return {}


def longtask_create_handler(args: Dict[str, Any], **kw) -> str:
    try:
        parent = kw.get("parent_agent")
        sid = _session_id(parent, args.get("session_id"), kw.get("session_id"))
        board = create_board(
            _root(parent, args.get("root")),
            sid,
            args.get("objective") or "",
            args.get("nodes") or [],
            overwrite=bool(args.get("overwrite", False)),
        )
        return _ok({
            "status": "created",
            "session_id": sid,
            "task_id": board["task_id"],
            "objective": board["objective"],
            "next_ready": board["global_state"]["next_ready"],
            "node_count": len(board["nodes"]),
        })
    except Exception as exc:
        return _handle_error(exc)


def longtask_add_node_handler(args: Dict[str, Any], **kw) -> str:
    try:
        parent = kw.get("parent_agent")
        sid = _session_id(parent, args.get("session_id"), kw.get("session_id"))
        data = add_nodes(
            _root(parent, args.get("root")),
            sid,
            args.get("nodes") or [],
        )
        return _ok({"status": "ok", "session_id": sid, **data})
    except Exception as exc:
        return _handle_error(exc)


def longtask_cancel_node_handler(args: Dict[str, Any], **kw) -> str:
    try:
        parent = kw.get("parent_agent")
        sid = _session_id(parent, args.get("session_id"), kw.get("session_id"))
        data = cancel_node(
            _root(parent, args.get("root")),
            sid,
            str(args.get("node_id") or ""),
            reason=args.get("reason"),
        )
        return _ok({"status": "ok", "session_id": sid, **data})
    except Exception as exc:
        return _handle_error(exc)


def longtask_read_handler(args: Dict[str, Any], **kw) -> str:
    try:
        parent = kw.get("parent_agent")
        sid = _session_id(parent, args.get("session_id"), kw.get("session_id"))
        data = read_board(
            _root(parent, args.get("root")),
            sid,
            node_id=args.get("node_id"),
        )
        return _ok({"status": "ok", "session_id": sid, "board": data})
    except Exception as exc:
        return _handle_error(exc)


def longtask_next_handler(args: Dict[str, Any], **kw) -> str:
    try:
        parent = kw.get("parent_agent")
        sid = _session_id(parent, args.get("session_id"), kw.get("session_id"))
        limit = args.get("limit")
        if limit is not None:
            limit = int(limit)
        data = next_ready_nodes(_root(parent, args.get("root")), sid, limit=limit)
        return _ok({"status": "ok", "session_id": sid, **data})
    except Exception as exc:
        return _handle_error(exc)


def longtask_update_node_handler(args: Dict[str, Any], **kw) -> str:
    try:
        parent = kw.get("parent_agent")
        sid = _session_id(parent, args.get("session_id"), kw.get("session_id"))
        data = update_node(
            _root(parent, args.get("root")),
            sid,
            node_id=str(args.get("node_id") or ""),
            resolution=args.get("resolution"),
            blocked_reason=args.get("blocked_reason"),
            goal=args.get("goal"),
            dependencies=args.get("dependencies"),
            assigned_to=args.get("assigned_to"),
            claims=args.get("claims"),
            evidence=args.get("evidence"),
            verification=args.get("verification"),
            notes=args.get("notes"),
        )
        return _ok({"status": "ok", "session_id": sid, **data})
    except Exception as exc:
        return _handle_error(exc)


def longtask_attach_report_handler(args: Dict[str, Any], **kw) -> str:
    try:
        parent = kw.get("parent_agent")
        sid = _session_id(parent, args.get("session_id"), kw.get("session_id"))
        report = args.get("report")
        if not isinstance(report, dict):
            raise LongtaskBoardError("report must be an object")
        data = attach_report(
            _root(parent, args.get("root")),
            sid,
            str(args.get("node_id") or ""),
            report,
            report_status=args.get("report_status"),
        )
        return _ok({"status": "ok", "session_id": sid, **data})
    except Exception as exc:
        return _handle_error(exc)


def longtask_verify_node_handler(args: Dict[str, Any], **kw) -> str:
    try:
        parent = kw.get("parent_agent")
        sid = _session_id(parent, args.get("session_id"), kw.get("session_id"))
        root = _root(parent, args.get("root"))
        board = load_board(root, sid)
        node_id = str(args.get("node_id") or "")
        node = next((n for n in board["nodes"] if n["node_id"] == node_id), None)
        if node is None:
            raise LongtaskBoardError(f"Unknown node_id: {node_id}")

        report = args.get("report")
        if report is None:
            report_path = node.get("report_path")
            if not report_path:
                raise LongtaskBoardError(f"Node {node_id} has no report to verify")
            path = board_dir(root, sid) / str(report_path)
            report = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise LongtaskBoardError("report must be an object")

        longtask_cfg = _load_longtask_config()
        verifier_cfg = longtask_cfg.get("verifier") or {}
        if not isinstance(verifier_cfg, dict):
            verifier_cfg = {}
        verification = verify_report_with_llm(
            report,
            node_goal=str(node.get("goal") or ""),
            objective=str(board.get("objective") or ""),
            workspace_root=_workspace_root(parent),
            llm_config=verifier_cfg,
        )
        updated = update_node(root, sid, node_id, verification=verification)
        return _ok({
            "status": "ok",
            "session_id": sid,
            "verification": verification,
            **updated,
        })
    except Exception as exc:
        return _handle_error(exc)


_COMMON_OPTIONAL = {
    "session_id": {
        "type": "string",
        "description": "Optional session id override. Omit to use the active Hermes session.",
    },
    "root": {
        "type": "string",
        "description": "Optional task-board root directory. Omit for <workspace>/.hermes/tasks.",
    },
}


registry.register(
    name="longtask_create",
    toolset="longtask",
    schema={
        "name": "longtask_create",
        "description": (
            "Create a durable long-horizon task board. Use for complex tasks "
            "that need DAG planning, subagent scheduling, or state that must "
            "survive context compression."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "Global objective."},
                "nodes": {
                    "type": "array",
                    "description": "Task graph nodes in any order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "node_id": {"type": "string"},
                            "goal": {"type": "string"},
                            "dependencies": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["node_id", "goal"],
                    },
                },
                "overwrite": {"type": "boolean"},
                **_COMMON_OPTIONAL,
            },
            "required": ["objective", "nodes"],
        },
    },
    handler=_board_handler(longtask_create_handler),
)

registry.register(
    name="longtask_add_node",
    toolset="longtask",
    schema={
        "name": "longtask_add_node",
        "description": (
            "Append items to an existing board when new work appears mid-run "
            "(replanning) — do not rebuild the board. Appended items may depend "
            "on existing ones; duplicate ids, unknown dependencies and cycles "
            "are rejected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nodes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "node_id": {"type": "string"},
                            "goal": {"type": "string"},
                            "dependencies": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["node_id", "goal"],
                    },
                },
                **_COMMON_OPTIONAL,
            },
            "required": ["nodes"],
        },
    },
    handler=_board_handler(longtask_add_node_handler),
)

registry.register(
    name="longtask_cancel_node",
    toolset="longtask",
    schema={
        "name": "longtask_cancel_node",
        "description": (
            "Cancel a board item that is superseded or no longer worth doing. "
            "Its dependents are NOT cancelled for you: they come back in "
            "dependents_to_review and will report the cancelled dependency, so "
            "rewire them (longtask_update_node with new dependencies) or cancel "
            "them explicitly. That decision stays visible."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "reason": {
                    "type": "string",
                    "description": "Why the item is cancelled; recorded on the item.",
                },
                **_COMMON_OPTIONAL,
            },
            "required": ["node_id"],
        },
    },
    handler=_board_handler(longtask_cancel_node_handler),
)

registry.register(
    name="longtask_read",
    toolset="longtask",
    schema={
        "name": "longtask_read",
        "description": "Read the current long-horizon task board or one node.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "Optional node id."},
                **_COMMON_OPTIONAL,
            },
            "required": [],
        },
    },
    handler=_board_handler(longtask_read_handler),
)

registry.register(
    name="longtask_next",
    toolset="longtask",
    schema={
        "name": "longtask_next",
        "description": (
            "Return the ready frontier: items whose dependencies are resolved "
            "and that are open, plus the items still waiting and what they wait "
            "on. Use this before delegating work to subagents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1},
                **_COMMON_OPTIONAL,
            },
            "required": [],
        },
    },
    handler=_board_handler(longtask_next_handler),
)

registry.register(
    name="longtask_update_node",
    toolset="longtask",
    schema={
        "name": "longtask_update_node",
        "description": (
            "Update a task-board item's resolution (open | in_progress | "
            "resolved | cancelled), its blocker, or its claims/evidence. "
            "`resolved` asserts the result was returned AND sufficiently "
            "checked; with longtask.require_verification_before_unlock it "
            "requires an accepted verdict first. Execution status is "
            "runtime-owned and cannot be set here."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "resolution": {
                    "type": "string",
                    "enum": ["open", "in_progress", "resolved", "cancelled"],
                },
                "blocked_reason": {
                    "type": "string",
                    "description": (
                        "Set when the item is waiting on something outside your "
                        "control (a human decision, a missing credential). Keeps "
                        "the item open but out of the ready frontier."
                    ),
                },
                "goal": {
                    "type": "string",
                    "description": (
                        "Revise the item's description when the plan changes. "
                        "Plan revisions are edits to the board, not a rebuild."
                    ),
                },
                "dependencies": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Replace this item's dependencies (rewiring). Unknown "
                        "ids and cycles are rejected."
                    ),
                },
                "assigned_to": {"type": "string"},
                "claims": {"type": "array", "items": {"type": "object"}},
                "evidence": {"type": "array", "items": {"type": "object"}},
                "verification": {"type": "object"},
                "notes": {"type": "string"},
                **_COMMON_OPTIONAL,
            },
            "required": ["node_id"],
        },
    },
    handler=_board_handler(longtask_update_node_handler),
)

registry.register(
    name="longtask_attach_report",
    toolset="longtask",
    schema={
        "name": "longtask_attach_report",
        "description": (
            "Attach a structured subagent report to a board item. The full "
            "report is persisted to disk and a bounded preview stays on the "
            "board. Attaching records the report and marks the execution as "
            "reported — it does NOT resolve the item: resolve it explicitly "
            "after longtask_verify_node."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "report": {"type": "object"},
                "report_status": {
                    "type": "string",
                    "description": (
                        "Optional advisory label from the child "
                        "(success|partial|failed|timeout). Recorded for the "
                        "reader; it never advances the item's resolution."
                    ),
                },
                **_COMMON_OPTIONAL,
            },
            "required": ["node_id", "report"],
        },
    },
    handler=_board_handler(longtask_attach_report_handler),
)

registry.register(
    name="longtask_verify_node",
    toolset="longtask",
    schema={
        "name": "longtask_verify_node",
        "description": (
            "Run deterministic verification over a node's claim-evidence "
            "report and write the verification result back to the board."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "report": {
                    "type": "object",
                    "description": (
                        "Optional report object. Omit to verify the node's "
                        "attached report."
                    ),
                },
                **_COMMON_OPTIONAL,
            },
            "required": ["node_id"],
        },
    },
    handler=_board_handler(longtask_verify_node_handler),
)
