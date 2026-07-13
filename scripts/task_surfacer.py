#!/usr/bin/env python3
"""Task Surfacer — checks every 5 minutes for due tasks.

Reads ``$HERMES_HOME/thoughts.json``, finds active tasks whose absolute
scheduled_at has arrived (with optional 'schedule_cron' fallback for older
records), and prints reminders to stdout for cron delivery.

Recurring tasks (recurrence != 'once') advance to the next occurrence in
schedule_cron on fire; one-shot tasks flip state='done'.

Designed to run as a ``no_agent=True`` cron job.

Silent (empty stdout) when nothing is due.

Environment:
  HERMES_HOME  — profile home; defaults to ~/.hermes.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def _store_path() -> Path:
    return _hermes_home() / "thoughts.json"


def _save_store(store: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _cron_matches(cron_expr: str, now: datetime) -> bool:
    """Legacy cron matcher — kept for tasks that only have schedule_cron."""
    if not cron_expr:
        return False
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return False
    fields = [
        (parts[0], now.minute),
        (parts[1], now.hour),
        (parts[2], now.day),
        (parts[3], now.month),
        (parts[4], now.weekday()),
    ]
    for expr, val in fields:
        if expr == "*":
            continue
        if "," in expr:
            allowed = [int(x.strip()) for x in expr.split(",") if x.strip().isdigit()]
            if val not in allowed:
                return False
        elif expr.isdigit():
            if int(expr) != val:
                return False
        else:
            return False
    return True


def _parse_iso(dt_str) -> datetime | None:
    if not isinstance(dt_str, str) or not dt_str.strip():
        return None
    try:
        return datetime.fromisoformat(dt_str.strip().replace("Z", "+00:00"))
    except Exception:
        return None


def _now_local(tz_hint: datetime | None = None) -> datetime:
    if tz_hint is not None and tz_hint.tzinfo is not None:
        return datetime.now(tz_hint.tzinfo)
    # Default to CST for legacy tasks lacking tz info.
    return datetime.now(timezone(timedelta(hours=8)))


def _advance_recurrence(scheduled: datetime, recurrence: str) -> datetime | None:
    """Bump scheduled_at to the next occurrence for a recurring task."""
    r = (recurrence or "once").lower()
    if r == "daily":
        return scheduled + timedelta(days=1)
    if r == "weekly":
        return scheduled + timedelta(days=7)
    if r == "monthly":
        # Rough +1 month; good enough for reminders.
        month = scheduled.month + 1
        year = scheduled.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        try:
            return scheduled.replace(year=year, month=month)
        except ValueError:
            # e.g. Jan 31 → Feb 31 doesn't exist. Fall back to last day of month.
            import calendar
            last = calendar.monthrange(year, month)[1]
            return scheduled.replace(year=year, month=month, day=last)
    if r == "yearly":
        try:
            return scheduled.replace(year=scheduled.year + 1)
        except ValueError:
            return scheduled + timedelta(days=365)
    return None


def _format_reminder(task: dict) -> str:
    lines = [f"⏰ Reminder: {task.get('title', 'Untitled')}"]
    notes = task.get("notes")
    if notes:
        lines.append(f"📝 {notes}")
    return "\n".join(lines) + "\n"


def main() -> None:
    path = _store_path()
    if not path.exists():
        return
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    now = _now_local()
    now_utc = datetime.now(timezone.utc)
    output_lines = []
    changed = False

    for t in store.get("tasks", []):
        if t.get("state") != "active":
            continue

        scheduled = _parse_iso(t.get("scheduled_at"))
        fired = False

        if scheduled is not None:
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=now.tzinfo)
            if now >= scheduled:
                fired = True
        else:
            # Legacy fallback for tasks without scheduled_at: honour a
            # populated schedule_cron string using the old wall-clock match.
            cron = t.get("schedule_cron", "") or ""
            if cron and _cron_matches(cron, now):
                fired = True

        if not fired:
            continue

        output_lines.append(_format_reminder(t))
        t["last_reminded"] = now_utc.isoformat()
        t["remind_count"] = (t.get("remind_count", 0) or 0) + 1
        changed = True

        recurrence = (t.get("recurrence") or "once").lower()
        if recurrence == "once":
            t["state"] = "done"
        elif scheduled is not None:
            nxt = _advance_recurrence(scheduled, recurrence)
            if nxt is not None:
                t["scheduled_at"] = nxt.isoformat(timespec="seconds")

    if changed:
        _save_store(store)

    if output_lines:
        sys.stdout.write("\n".join(output_lines).strip())
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
