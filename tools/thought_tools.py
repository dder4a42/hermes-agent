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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.registry import registry

try:
    from hermes_constants import get_hermes_home
except ImportError:
    get_hermes_home = lambda: Path.home() / ".hermes"

logger = logging.getLogger("thought_tools")

SCHEMA_VERSION = 1
DEFAULT_REVIEW_DAYS = 7


# ── Storage helpers ───────────────────────────────────────────────────────────

def _store_path() -> Path:
    return get_hermes_home() / "thoughts.json"


def _load_store() -> dict:
    path = _store_path()
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "thoughts": [], "tasks": []}
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(store, dict) or store.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                "Unsupported thoughts.json schema. Run "
                "`python scripts/migrate_thoughts.py` before starting Hermes."
            )
        return store
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt thoughts.json, resetting")
        return {"schema_version": SCHEMA_VERSION, "thoughts": [], "tasks": []}


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

_MAX_CHECKLIST_ITEMS = 20


def _normalize_checklist(items):
    """Return a list of checklist item dicts from raw input.

    Accepts a list of strings, a list of dicts (with text/done keys), or None.
    Deduplicates by text (case-insensitive) and caps at _MAX_CHECKLIST_ITEMS
    so a runaway extractor cannot balloon a task.
    """
    if not items:
        return []
    seen = set()
    out = []
    for entry in items:
        if isinstance(entry, str):
            text = entry.strip()
            done = False
            checked_at = None
        elif isinstance(entry, dict):
            text = str(entry.get("text", "")).strip()
            done = bool(entry.get("done"))
            checked_at = entry.get("checked_at")
        else:
            continue
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "id": _new_id("ci"),
            "text": text,
            "done": done,
            "checked_at": checked_at,
        })
        if len(out) >= _MAX_CHECKLIST_ITEMS:
            break
    return out


def _normalize_str_list(values):
    if not values:
        return []
    if isinstance(values, str):
        values = [v.strip() for v in values.split(",")]
    return [str(v).strip() for v in values if str(v).strip()]


def _normalize_remind_before(value):
    """Convert user-facing lead-time expressions to integer minutes.

    Accepts int, or strings like '30m', '1h', '30分钟', '2小时', '1天'.
    Bare digits are treated as minutes.
    """
    if value in (None, "", 0, "0"):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    s = str(value).strip().lower()
    if not s:
        return 0
    import re as _re
    m = _re.match(r"^(\d+)\s*(m|min|mins|minute|minutes|分|分钟)?$", s)
    if m:
        return max(0, int(m.group(1)))
    m = _re.match(r"^(\d+)\s*(h|hr|hrs|hour|hours|小时)$", s)
    if m:
        return max(0, int(m.group(1)) * 60)
    m = _re.match(r"^(\d+)\s*(d|day|days|天)$", s)
    if m:
        return max(0, int(m.group(1)) * 60 * 24)
    return 0


def add_task(
    title: str,
    schedule_raw: str,
    notes: str = "",
    recurrence: str = "once",
    *,
    url: str = "",
    location: str = "",
    attendees=None,
    tags=None,
    remind_before_min=0,
    checklist=None,
    input_raw: str = "",
) -> dict:
    """Create a concrete timed reminder. Returns the saved task dict.

    Resolves schedule_raw ('tomorrow 3pm', '明天下午3点', '每周三下午2点', ...)
    into an absolute scheduled_at via tools.schedule_parser. If the parser
    fails (network down, ambiguous phrasing, invalid time), the task is still
    saved with scheduled_at=None and a parse_error string so the surfacer
    stays silent; the user can update the schedule later.

    Optional structured fields (any subset may be provided):
        url               join link / doc link.
        location          physical or virtual place.
        attendees         list[str] or comma-separated string.
        tags              list[str] or comma-separated string.
        remind_before_min minutes before scheduled_at for a pre-reminder.
                          Accepts int OR strings like '30m', '1h', '2小时'.
        checklist         list of prep items -- bare strings or
                          {text, done} dicts. Deduped + capped at 20.
        input_raw         verbatim user text (all turns concatenated) so we
                          can audit how the LLM rewrote it into title/notes/
                          checklist. Never surfaced in reminders, only in
                          debug tooling.
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
        "input_raw": (input_raw or "").strip(),
        "url": (url or "").strip(),
        "location": (location or "").strip(),
        "attendees": _normalize_str_list(attendees),
        "tags": _normalize_str_list(tags),
        "remind_before_min": _normalize_remind_before(remind_before_min),
        "checklist": _normalize_checklist(checklist),
        "parse": {
            "error": parse_error,
            "confidence": parse_confidence,
            "reasoning": parse_reasoning,
        },
    }
    store.setdefault("tasks", []).append(task)
    _save_store(store)
    return task




def _find_task(store, task_id: str):
    for t in store.get("tasks", []):
        if t["id"] == task_id:
            return t
    return None


def _resolve_item(task: dict, ref):
    """Find a checklist item by 1-based index, id, or id-prefix.

    Returns (index, item) or (-1, None) if not found.
    """
    items = task.get("checklist") or []
    if not items:
        return -1, None
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        idx = int(ref) - 1
        if 0 <= idx < len(items):
            return idx, items[idx]
        return -1, None
    if not isinstance(ref, str):
        return -1, None
    ref = ref.strip()
    for i, it in enumerate(items):
        if it.get("id") == ref:
            return i, it
    for i, it in enumerate(items):
        if it.get("id", "").startswith(ref) or it.get("text", "").lower().startswith(ref.lower()):
            return i, it
    return -1, None


def check_task_item(task_id: str, item_ref) -> bool:
    """Mark a checklist item done. Returns True if found and updated."""
    store = _load_store()
    task = _find_task(store, task_id)
    if task is None:
        return False
    idx, item = _resolve_item(task, item_ref)
    if item is None:
        return False
    item["done"] = True
    item["checked_at"] = _now_iso()
    _save_store(store)
    return True


def uncheck_task_item(task_id: str, item_ref) -> bool:
    store = _load_store()
    task = _find_task(store, task_id)
    if task is None:
        return False
    idx, item = _resolve_item(task, item_ref)
    if item is None:
        return False
    item["done"] = False
    item["checked_at"] = None
    _save_store(store)
    return True


def add_task_item(task_id: str, text: str) -> dict:
    """Append a new checklist item. Returns the item dict or None if the task
    doesn\'t exist / duplicate text / cap hit."""
    text = (text or "").strip()
    if not text:
        return None
    store = _load_store()
    task = _find_task(store, task_id)
    if task is None:
        return None
    existing = task.setdefault("checklist", [])
    lowered = {(it.get("text") or "").strip().lower() for it in existing}
    if text.lower() in lowered:
        return None
    if len(existing) >= _MAX_CHECKLIST_ITEMS:
        return None
    item = {
        "id": _new_id("ci"),
        "text": text,
        "done": False,
        "checked_at": None,
    }
    existing.append(item)
    _save_store(store)
    return item


def remove_task_item(task_id: str, item_ref) -> bool:
    store = _load_store()
    task = _find_task(store, task_id)
    if task is None:
        return False
    idx, item = _resolve_item(task, item_ref)
    if item is None:
        return False
    del task["checklist"][idx]
    _save_store(store)
    return True
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


def capture_thought(
    title: str,
    summary: str,
    source: str = "",
    tags: Optional[List[str]] = None,
    *,
    next_action_kind: str = "clarify",
    next_action_prompt: str = "",
    next_review_at: Optional[str] = None,
    priority: str = "normal",
) -> dict:
    """Save a fuzzy idea for later incubation. Returns the saved thought dict."""
    store = _load_store()
    now = datetime.now(timezone.utc)
    if next_review_at:
        try:
            parsed_review = datetime.fromisoformat(str(next_review_at).replace("Z", "+00:00"))
            if parsed_review.tzinfo is None:
                parsed_review = parsed_review.replace(tzinfo=timezone.utc)
            next_review_at = parsed_review.isoformat()
        except ValueError:
            next_review_at = None
    if not next_review_at:
        next_review_at = (now + timedelta(days=DEFAULT_REVIEW_DAYS)).isoformat()
    if next_action_kind not in {"clarify", "research", "learn", "track", "defer"}:
        next_action_kind = "clarify"
    if priority not in {"low", "normal", "high"}:
        priority = "normal"
    thought = {
        "id": _new_id("th"),
        "title": title,
        "summary": summary,
        "source": source or "",
        "tags": tags or [],
        "state": "active",
        "stage": "fuzzy",
        "priority": priority,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "surfaced_at": None,
        "surface_count": 0,
        "open_questions": [],
        "next_action": {
            "kind": next_action_kind,
            "prompt": (next_action_prompt or "").strip(),
        },
        "review": {
            "next_review_at": next_review_at,
            "cadence": f"{DEFAULT_REVIEW_DAYS}d",
            "snoozed_until": None,
            "unanswered_count": 0,
        },
        "progress": {
            "status": "unexplored",
            "last_update_at": None,
        },
        "history": [{"at": now.isoformat(), "event": "captured"}],
    }
    store.setdefault("thoughts", []).append(thought)
    _save_store(store)
    return thought


def capture_thought_text(text: str, *, source: str = "command") -> dict:
    """Capture free text without a second model call.

    The full text remains the summary; the first non-empty line becomes a
    compact title. Normal agent turns can still call ``thought_capture`` with
    a model-written title/summary, while command-only gateways get a reliable
    capture path that never drops the user's wording.
    """
    summary = (text or "").strip()
    if not summary:
        raise ValueError("thought text is required")
    first_line = next((line.strip() for line in summary.splitlines() if line.strip()), summary)
    title = first_line[:60].rstrip("，。,.!?！？;； ")
    if len(first_line) > 60:
        title += "…"
    return capture_thought(title=title, summary=summary, source=source)


def _find_thought(store: dict, thought_id: str) -> Optional[dict]:
    return next((th for th in store.get("thoughts", []) if th.get("id") == thought_id), None)


def update_thought_action(thought_id: str, kind: str, prompt: str = "") -> bool:
    """Set the next incubation action without executing it."""
    if kind not in {"clarify", "research", "learn", "track", "defer"}:
        return False
    store = _load_store()
    thought = _find_thought(store, thought_id)
    if thought is None:
        return False
    now = _now_iso()
    thought["next_action"] = {"kind": kind, "prompt": (prompt or "").strip()}
    thought["updated_at"] = now
    thought.setdefault("history", []).append({"at": now, "event": "next_action", "kind": kind})
    _save_store(store)
    return True


def snooze_thought(thought_id: str, until: str) -> bool:
    """Defer a thought until an ISO timestamp or a compact duration (3d/12h)."""
    text = (until or "").strip().lower()
    now = datetime.now(timezone.utc)
    import re as _re
    match = _re.fullmatch(r"(\d+)\s*([hd])", text)
    if match:
        amount = int(match.group(1))
        target = now + (timedelta(hours=amount) if match.group(2) == "h" else timedelta(days=amount))
    else:
        try:
            target = datetime.fromisoformat(text.replace("z", "+00:00"))
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
    store = _load_store()
    thought = _find_thought(store, thought_id)
    if thought is None:
        return False
    target_iso = target.isoformat()
    thought.setdefault("review", {})["snoozed_until"] = target_iso
    thought["review"]["next_review_at"] = target_iso
    thought["updated_at"] = now.isoformat()
    thought.setdefault("history", []).append({"at": now.isoformat(), "event": "snoozed", "until": target_iso})
    _save_store(store)
    return True


def record_thought_response(thought_id: str, text: str) -> bool:
    """Record that the user engaged with a surfaced thought."""
    store = _load_store()
    thought = _find_thought(store, thought_id)
    if thought is None:
        return False
    now = datetime.now(timezone.utc)
    thought["stage"] = "exploring"
    thought["updated_at"] = now.isoformat()
    thought.setdefault("progress", {})["status"] = "engaged"
    thought["progress"]["last_update_at"] = now.isoformat()
    review = thought.setdefault("review", {})
    review["unanswered_count"] = 0
    review["snoozed_until"] = None
    review["next_review_at"] = (now + timedelta(days=DEFAULT_REVIEW_DAYS)).isoformat()
    thought.setdefault("history", []).append({
        "at": now.isoformat(), "event": "user_response", "text": (text or "").strip()[:1000],
    })
    _save_store(store)
    return True


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
            review = th.setdefault("review", {})
            unanswered = int(review.get("unanswered_count") or 0) + 1
            review["unanswered_count"] = unanswered
            # Gentle exponential backoff: 3d, 7d, then 30d.
            delay_days = (3, 7, 30)[min(unanswered - 1, 2)]
            review["next_review_at"] = (
                datetime.now(timezone.utc) + timedelta(days=delay_days)
            ).isoformat()
            review["snoozed_until"] = None
            th["updated_at"] = _now_iso()
            th.setdefault("history", []).append({"at": _now_iso(), "event": "surfaced"})
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




def parse_s_add_args(raw: str) -> dict:
    """Tokenise the argument tail of into a flat dict.

    Shared by the CLI () and the gateway
    () so both surfaces accept the same flags.

    Supported flags (all optional except --when):
        --when "..."          schedule expression   (required)
        --notes "..."         free-form notes
        --url URL              join/doc link
        --where / --location   physical or virtual place
        --attendees a,b,c      comma-separated
        --tags t1,t2           comma-separated
        --remind-before / --lead  lead-time spec (30m, 1h, ...)
        --checklist "a;b;c"    semicolon-separated

    Everything else is joined back into the title. Uses shlex so quoted
    values survive intact. Returns raw strings — actual normalisation
    (attendee dedup, remind-before -> minutes) happens inside add_task.
    """
    import shlex
    text = (raw or "").strip()
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()

    flags = {
        "--when": "schedule_raw",
        "--notes": "notes",
        "--url": "url",
        "--where": "location",
        "--location": "location",
        "--attendees": "attendees",
        "--with": "attendees",
        "--tags": "tags",
        "--remind-before": "remind_before",
        "--lead": "remind_before",
        "--checklist": "checklist",
    }
    out = {
        "title": "",
        "schedule_raw": "",
        "notes": "",
        "url": "",
        "location": "",
        "attendees": [],
        "tags": [],
        "remind_before": "",
        "checklist": [],
    }
    title_tokens = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        target = flags.get(tok)
        if target is None:
            title_tokens.append(tok)
            i += 1
            continue
        i += 1
        if i >= len(tokens):
            break
        value = tokens[i]
        if target in ("attendees", "tags"):
            out[target] = [v.strip() for v in value.split(",") if v.strip()]
        elif target == "checklist":
            out[target] = [v.strip() for v in value.split(";") if v.strip()]
        else:
            out[target] = value
        i += 1

    out["title"] = " ".join(title_tokens).strip().strip('\"\'')
    return out

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
        next_action_kind=args.get("next_action_kind", "clarify"),
        next_action_prompt=args.get("next_action_prompt", ""),
        next_review_at=args.get("next_review_at"),
        priority=args.get("priority", "normal"),
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
        "next_action_kind": {"type": "string", "enum": ["clarify", "research", "learn", "track", "defer"], "description": "How to advance the idea at its next review"},
        "next_action_prompt": {"type": "string", "description": "Optional specific question to ask at the next review"},
        "next_review_at": {"type": "string", "description": "Optional ISO-8601 review time; defaults to seven days from capture"},
        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
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
            "properties": {k: v for k, v in _thought_schema["properties"].items() if k in ("title", "summary", "source", "tags", "next_action_kind", "next_action_prompt", "next_review_at", "priority")},
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
