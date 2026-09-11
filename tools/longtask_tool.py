#!/usr/bin/env python3
"""Long-horizon task board tools.

These tools keep DAG state outside the model context so the main agent can
schedule subagents by dependency order and survive context compression.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from agent.longtask_board import (
    LongtaskBoardError,
    attach_report,
    board_dir,
    create_board,
    load_board,
    next_ready_nodes,
    read_board,
    update_node,
)
from agent.longtask_verifier import verify_report_with_llm
from tools.registry import registry, tool_error


def _workspace_root(parent_agent: Any = None) -> Path:
    candidates = []
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


def _session_id(parent_agent: Any = None, explicit: Optional[str] = None) -> str:
    if explicit:
        return str(explicit)
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
        sid = _session_id(parent, args.get("session_id"))
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


def longtask_read_handler(args: Dict[str, Any], **kw) -> str:
    try:
        parent = kw.get("parent_agent")
        sid = _session_id(parent, args.get("session_id"))
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
        sid = _session_id(parent, args.get("session_id"))
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
        sid = _session_id(parent, args.get("session_id"))
        data = update_node(
            _root(parent, args.get("root")),
            sid,
            str(args.get("node_id") or ""),
            status=args.get("status"),
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
        sid = _session_id(parent, args.get("session_id"))
        report = args.get("report")
        if not isinstance(report, dict):
            raise LongtaskBoardError("report must be an object")
        data = attach_report(
            _root(parent, args.get("root")),
            sid,
            str(args.get("node_id") or ""),
            report,
            status=args.get("status"),
        )
        return _ok({"status": "ok", "session_id": sid, **data})
    except Exception as exc:
        return _handle_error(exc)


def longtask_verify_node_handler(args: Dict[str, Any], **kw) -> str:
    try:
        parent = kw.get("parent_agent")
        sid = _session_id(parent, args.get("session_id"))
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
    handler=longtask_create_handler,
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
    handler=longtask_read_handler,
)

registry.register(
    name="longtask_next",
    toolset="longtask",
    schema={
        "name": "longtask_next",
        "description": (
            "Return nodes whose dependencies are done and are ready to execute. "
            "Use this before delegating work to subagents."
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
    handler=longtask_next_handler,
)

registry.register(
    name="longtask_update_node",
    toolset="longtask",
    schema={
        "name": "longtask_update_node",
        "description": (
            "Update a task-board node. Status transitions and dependencies are "
            "validated by Hermes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": [
                        "pending",
                        "ready",
                        "running",
                        "blocked",
                        "done",
                        "failed",
                        "timeout",
                        "cancelled",
                    ],
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
    handler=longtask_update_node_handler,
)

registry.register(
    name="longtask_attach_report",
    toolset="longtask",
    schema={
        "name": "longtask_attach_report",
        "description": (
            "Attach a structured subagent report to a node. Full report is "
            "persisted to disk and a bounded preview is kept on the board."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "report": {"type": "object"},
                "status": {
                    "type": "string",
                    "enum": ["done", "failed", "timeout", "blocked"],
                },
                **_COMMON_OPTIONAL,
            },
            "required": ["node_id", "report"],
        },
    },
    handler=longtask_attach_report_handler,
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
    handler=longtask_verify_node_handler,
)
