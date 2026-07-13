#!/usr/bin/env python3
"""
Thought & Task Tools — persistent idea incubation + concrete reminders.

Two data types stored in ``~/.hermes/thoughts.json``:

  **thoughts:** fuzzy, evolving ideas ("we should invest in GUI agents")
  **tasks:** concrete timed reminders ("try Mixue's new drink tomorrow")

This file exports both Hermes agent tools (task_add, thought_capture, …)
and pure-Python helpers that the cron surfer scripts import.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import registry

try:
    from hermes_constants import get_hermes_home
except ImportError:
    get_hermes_home = lambda: Path.home() / ".hermes"

logger = logging.getLogger("thought_tools")


# ── Storage helpers ───────────────────────────────────────────────────────────

def _store_path() -> Path:
    return get_hermes_home() / "thoughts.json"


def _load_store() -> dict:
    path = _store_path()
    if not path.exists():
        return {"thoughts": [], "tasks": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt thoughts.json, resetting")
        return {"thoughts": [], "tasks": []}


def _save_store(store: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ── Public helpers (imported by cron scripts and tests) ───────────────────────

def add_task(title: str, schedule_raw: str, notes: str = "", recurrence: str = "once") -> dict:
    """Create a concrete timed reminder. Returns the saved task dict.

    Resolves schedule_raw ("tomorrow 3pm", "明天下午3点", "每周三下午2点", …)
    into an absolute scheduled_at via tools.schedule_parser. If the parser
    fails (network down, ambiguous phrasing, invalid time), the task is still
    saved with scheduled_at=None and a parse_error string so the surfacer
    stays silent and the user can update the schedule later.
    """
    from tools.schedule_parser import parse_schedule

    result = parse_schedule(schedule_raw)

    resolved_recurrence = recurrence
    scheduled_at = None
    schedule_cron = ""
    parse_error = None
    parse_confidence = None
    parse_reasoning = None

    if result.ok:
        scheduled_at = result.scheduled_at
        schedule_cron = result.schedule_cron or ""
        # Trust the LLM when the caller passed the default 'once'; caller-set
        # non-default recurrence wins (explicit intent from CLI/tool arg).
        if recurrence == "once":
            resolved_recurrence = result.recurrence
        parse_confidence = result.confidence
        parse_reasoning = result.reasoning
    else:
        parse_error = result.error

    store = _load_store()
    task = {
        "id": _new_id("tk"),
        "title": title,
        "notes": notes or "",
        "schedule_raw": schedule_raw,
        "scheduled_at": scheduled_at,
        "schedule_cron": schedule_cron,
        "recurrence": resolved_recurrence,
        "state": "active",
        "created_at": _now_iso(),
        "last_reminded": None,
        "lead_reminded_at": None,
        "remind_count": 0,
        "parse": {
            "error": parse_error,
            "confidence": parse_confidence,
            "reasoning": parse_reasoning,
        },
    }
    store.setdefault("tasks", []).append(task)
    _save_store(store)
    return task


def done_task(task_id: str) -> bool:
    """Mark a task as done. Returns True if found."""
    store = _load_store()
    for t in store.setdefault("tasks", []):
        if t["id"] == task_id:
            t["state"] = "done"
            _save_store(store)
            return True
    return False


def remove_task(task_id: str) -> bool:
    """Permanently delete a task."""
    store = _load_store()
    before = len(store["tasks"])
    store["tasks"] = [t for t in store["tasks"] if t["id"] != task_id]
    if len(store["tasks"]) < before:
        _save_store(store)
        return True
    return False


def pause_task(task_id: str) -> bool:
    store = _load_store()
    for t in store.setdefault("tasks", []):
        if t["id"] == task_id and t["state"] != "done":
            t["state"] = "paused"
            _save_store(store)
            return True
    return False


def resume_task(task_id: str) -> bool:
    store = _load_store()
    for t in store.setdefault("tasks", []):
        if t["id"] == task_id:
            t["state"] = "active"
            _save_store(store)
            return True
    return False


def list_tasks(state: str = "") -> List[dict]:
    store = _load_store()
    tasks = store.get("tasks", [])
    if state:
        tasks = [t for t in tasks if t.get("state") == state]
    return tasks


def capture_thought(title: str, summary: str, source: str = "", tags: Optional[List[str]] = None) -> dict:
    """Save a fuzzy idea for later incubation. Returns the saved thought dict."""
    store = _load_store()
    thought = {
        "id": _new_id("th"),
        "title": title,
        "summary": summary,
        "source": source or "",
        "tags": tags or [],
        "state": "active",
        "created_at": _now_iso(),
        "surfaced_at": None,
        "surface_count": 0,
    }
    store.setdefault("thoughts", []).append(thought)
    _save_store(store)
    return thought


def archive_thought(thought_id: str) -> bool:
    store = _load_store()
    for th in store.setdefault("thoughts", []):
        if th["id"] == thought_id:
            th["state"] = "archived"
            _save_store(store)
            return True
    return False


def remove_thought(thought_id: str) -> bool:
    store = _load_store()
    before = len(store["thoughts"])
    store["thoughts"] = [th for th in store["thoughts"] if th["id"] != thought_id]
    if len(store["thoughts"]) < before:
        _save_store(store)
        return True
    return False


def pause_thought(thought_id: str) -> bool:
    store = _load_store()
    for th in store.setdefault("thoughts", []):
        if th["id"] == thought_id and th["state"] != "archived":
            th["state"] = "dormant"
            _save_store(store)
            return True
    return False


def resume_thought(thought_id: str) -> bool:
    store = _load_store()
    for th in store.setdefault("thoughts", []):
        if th["id"] == thought_id:
            th["state"] = "active"
            _save_store(store)
            return True
    return False


def list_thoughts(state: str = "") -> List[dict]:
    store = _load_store()
    thoughts = store.get("thoughts", [])
    if state:
        thoughts = [th for th in thoughts if th.get("state") == state]
    return thoughts


def mark_surfaced(thought_id: str) -> None:
    """Record that a thought has been surfaced (called by the surfer script)."""
    store = _load_store()
    for th in store.setdefault("thoughts", []):
        if th["id"] == thought_id:
            th["surfaced_at"] = _now_iso()
            th["surface_count"] = (th.get("surface_count", 0) or 0) + 1
            _save_store(store)
            return


def tasks_due() -> List[dict]:
    """Return active tasks whose schedule matches the current minute.

    This is a best-effort cron matcher. Returns tasks whose cron field
    matches ``* * * * *`` (every minute). For production cron matching
    we rely on the shell-level cron expression — this helper works for
    the no_agent=True script pattern where we deliver everything due.
    """
    store = _load_store()
    now = datetime.now(timezone.utc)
    due = []
    for t in store.get("tasks", []):
        if t.get("state") != "active":
            continue
        cron = t.get("schedule_cron", "")
        if not cron:
            continue
        parts = cron.strip().split()
        if len(parts) != 5:
            continue
        # Simple wildcard-only or exact-minute match for the surfer
        minute_ok = parts[0] == "*" or parts[0] == str(now.minute)
        hour_ok = parts[1] == "*" or parts[1] == str(now.hour)
        if minute_ok and hour_ok:
            due.append(t)
    return due


# ── Hermes Agent Tools ────────────────────────────────────────────────────────

def _handler_task_add(args: dict, task_id: str = None) -> str:
    task = add_task(
        title=args.get("title", "Untitled"),
        schedule_raw=args.get("schedule_raw", ""),
        notes=args.get("notes", ""),
        recurrence=args.get("recurrence", "once"),
    )
    return json.dumps({"success": True, "task": task})


def _handler_task_done(args: dict, task_id: str = None) -> str:
    ok = done_task(args.get("task_id", ""))
    return json.dumps({"success": ok})


def _handler_task_remove(args: dict, task_id: str = None) -> str:
    ok = remove_task(args.get("task_id", ""))
    return json.dumps({"success": ok})


def _handler_task_pause(args: dict, task_id: str = None) -> str:
    ok = pause_task(args.get("task_id", ""))
    return json.dumps({"success": ok})


def _handler_task_resume(args: dict, task_id: str = None) -> str:
    ok = resume_task(args.get("task_id", ""))
    return json.dumps({"success": ok})


def _handler_task_list(args: dict, task_id: str = None) -> str:
    tasks = list_tasks(state=args.get("state", ""))
    return json.dumps({"success": True, "tasks": tasks})


def _handler_thought_capture(args: dict, task_id: str = None) -> str:
    thought = capture_thought(
        title=args.get("title", "Untitled"),
        summary=args.get("summary", ""),
        source=args.get("source", ""),
        tags=args.get("tags"),
    )
    return json.dumps({"success": True, "thought": thought})


def _handler_thought_list(args: dict, task_id: str = None) -> str:
    thoughts = list_thoughts(state=args.get("state", ""))
    return json.dumps({"success": True, "thoughts": thoughts})


def _handler_thought_done(args: dict, task_id: str = None) -> str:
    ok = archive_thought(args.get("thought_id", ""))
    return json.dumps({"success": ok})


def _handler_thought_remove(args: dict, task_id: str = None) -> str:
    ok = remove_thought(args.get("thought_id", ""))
    return json.dumps({"success": ok})


def _handler_thought_pause(args: dict, task_id: str = None) -> str:
    ok = pause_thought(args.get("thought_id", ""))
    return json.dumps({"success": ok})


def _handler_thought_resume(args: dict, task_id: str = None) -> str:
    ok = resume_thought(args.get("thought_id", ""))
    return json.dumps({"success": ok})


def _make_handler(fn):
    """Wrap a handler so it serializes args correctly."""
    return lambda args, **kw: fn(args, task_id=kw.get("task_id"))


# ── Register tools ────────────────────────────────────────────────────────────

_task_schema = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Task ID (e.g. tk_abc12345)"},
        "title": {"type": "string", "description": "Task title / reminder text"},
        "schedule_raw": {"type": "string", "description": "Natural language schedule like 'tomorrow 2pm', 'every weekday at 9am'"},
        "notes": {"type": "string", "description": "Optional notes"},
        "recurrence": {"type": "string", "enum": ["once", "daily", "weekdays", "mon-fri", "weekends"], "description": "How often the task repeats"},
        "state": {"type": "string", "enum": ["active", "done", "paused"], "description": "Filter by state"},
    },
}

_thought_schema = {
    "type": "object",
    "properties": {
        "thought_id": {"type": "string", "description": "Thought ID (e.g. th_abc12345)"},
        "title": {"type": "string", "description": "Short title for the idea"},
        "summary": {"type": "string", "description": "Detailed description of the thought"},
        "source": {"type": "string", "description": "Where this idea came from (conversation topic, reading, etc.)"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags for organization"},
        "state": {"type": "string", "enum": ["active", "dormant", "archived"], "description": "Filter by state"},
    },
}

def _always() -> bool:
    return True

registry.register(
    name="task_add",
    toolset="scheduler",
    schema={
        "name": "task_add",
        "description": "Create a concrete timed reminder. Call this when the user says something like 'remind me to X tomorrow', 'schedule Y at 2pm', etc.",
        "parameters": {
            **{k: v for k, v in _task_schema.items() if k != "properties"},
            "properties": {k: v for k, v in _task_schema["properties"].items() if k in ("title", "schedule_raw", "notes", "recurrence")},
            "required": ["title", "schedule_raw"],
        },
    },
    handler=_make_handler(_handler_task_add),
    check_fn=_always,
)

registry.register(
    name="task_done",
    toolset="scheduler",
    schema={
        "name": "task_done",
        "description": "Mark a timed task as completed.",
        "parameters": {
            "type": "object",
            "properties": {"task_id": _task_schema["properties"]["task_id"]},
            "required": ["task_id"],
        },
    },
    handler=_make_handler(_handler_task_done),
    check_fn=_always,
)

registry.register(
    name="task_remove",
    toolset="scheduler",
    schema={
        "name": "task_remove",
        "description": "Permanently delete a task.",
        "parameters": {
            "type": "object",
            "properties": {"task_id": _task_schema["properties"]["task_id"]},
            "required": ["task_id"],
        },
    },
    handler=_make_handler(_handler_task_remove),
    check_fn=_always,
)

registry.register(
    name="task_pause",
    toolset="scheduler",
    schema={
        "name": "task_pause",
        "description": "Pause a task without deleting it.",
        "parameters": {
            "type": "object",
            "properties": {"task_id": _task_schema["properties"]["task_id"]},
            "required": ["task_id"],
        },
    },
    handler=_make_handler(_handler_task_pause),
    check_fn=_always,
)

registry.register(
    name="task_resume",
    toolset="scheduler",
    schema={
        "name": "task_resume",
        "description": "Resume a paused task.",
        "parameters": {
            "type": "object",
            "properties": {"task_id": _task_schema["properties"]["task_id"]},
            "required": ["task_id"],
        },
    },
    handler=_make_handler(_handler_task_resume),
    check_fn=_always,
)

registry.register(
    name="task_list",
    toolset="scheduler",
    schema={
        "name": "task_list",
        "description": "List tasks. Optionally filter by state (active, done, paused).",
        "parameters": {
            "type": "object",
            "properties": {"state": _task_schema["properties"]["state"]},
        },
    },
    handler=_make_handler(_handler_task_list),
    check_fn=_always,
)

registry.register(
    name="thought_capture",
    toolset="scheduler",
    schema={
        "name": "thought_capture",
        "description": "Save a fuzzy idea or plan for future incubation. Call this when the user discusses future plans, interesting directions, or vague intentions like 'we should invest in X', 'maybe we try Y'.",
        "parameters": {
            "type": "object",
            "properties": {k: v for k, v in _thought_schema["properties"].items() if k in ("title", "summary", "source", "tags")},
            "required": ["title", "summary"],
        },
    },
    handler=_make_handler(_handler_thought_capture),
    check_fn=_always,
)

registry.register(
    name="thought_list",
    toolset="scheduler",
    schema={
        "name": "thought_list",
        "description": "List saved thoughts. Optionally filter by state (active, dormant, archived).",
        "parameters": {
            "type": "object",
            "properties": {"state": _thought_schema["properties"]["state"]},
        },
    },
    handler=_make_handler(_handler_thought_list),
    check_fn=_always,
)

registry.register(
    name="thought_done",
    toolset="scheduler",
    schema={
        "name": "thought_done",
        "description": "Archive a thought (mark as done/dead).",
        "parameters": {
            "type": "object",
            "properties": {"thought_id": _thought_schema["properties"]["thought_id"]},
            "required": ["thought_id"],
        },
    },
    handler=_make_handler(_handler_thought_done),
    check_fn=_always,
)

registry.register(
    name="thought_remove",
    toolset="scheduler",
    schema={
        "name": "thought_remove",
        "description": "Permanently delete a thought.",
        "parameters": {
            "type": "object",
            "properties": {"thought_id": _thought_schema["properties"]["thought_id"]},
            "required": ["thought_id"],
        },
    },
    handler=_make_handler(_handler_thought_remove),
    check_fn=_always,
)

registry.register(
    name="thought_pause",
    toolset="scheduler",
    schema={
        "name": "thought_pause",
        "description": "Move a thought to dormant state (set aside for later).",
        "parameters": {
            "type": "object",
            "properties": {"thought_id": _thought_schema["properties"]["thought_id"]},
            "required": ["thought_id"],
        },
    },
    handler=_make_handler(_handler_thought_pause),
    check_fn=_always,
)

registry.register(
    name="thought_resume",
    toolset="scheduler",
    schema={
        "name": "thought_resume",
        "description": "Reactivate a dormant thought.",
        "parameters": {
            "type": "object",
            "properties": {"thought_id": _thought_schema["properties"]["thought_id"]},
            "required": ["thought_id"],
        },
    },
    handler=_make_handler(_handler_thought_resume),
    check_fn=_always,
)
