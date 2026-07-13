#!/usr/bin/env python3
"""Task Surfacer — checks every 5 minutes for due tasks.

Reads ``$HERMES_HOME/thoughts.json`` (defaults to ``~/.hermes/thoughts.json``),
finds active tasks whose schedule matches the current time, and prints
reminder messages to stdout.

Designed to run as a ``no_agent=True`` cron job.

Output format (stdout → WeChat delivery):
  ⏰ Reminder: <title>
  📝 <notes>  (if notes exist)

Silent (empty stdout) when nothing is due.

Environment variables:
  HERMES_HOME  — profile home; defaults to ~/.hermes. Set by the gateway so
                 per-profile cron jobs read/write the correct thoughts.json.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
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
    """Simple cron matcher — supports *, exact numbers, and comma lists."""
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
            return False  # unsupported (step values, ranges, etc.)
    return True


def main() -> None:
    path = _store_path()
    if not path.exists():
        return

    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    now = datetime.now(timezone.utc)
    due_tasks = []
    remaining_tasks = []

    for t in store.get("tasks", []):
        if t.get("state") != "active":
            remaining_tasks.append(t)
            continue
        cron = t.get("schedule_cron", "")
        if _cron_matches(cron, now):
            due_tasks.append(t)
        else:
            remaining_tasks.append(t)

    if not due_tasks:
        return

    output_lines = []
    for t in due_tasks:
        title = t.get("title", "Untitled")
        notes = t.get("notes", "")
        output_lines.append(f"⏰ Reminder: {title}")
        if notes:
            output_lines.append(f"📝 {notes}")
        output_lines.append("")  # blank line between tasks

        # Update last_reminded timestamp
        t["last_reminded"] = now.isoformat()
        t["remind_count"] = (t.get("remind_count", 0) or 0) + 1

        # One-shot tasks → auto-done after reminder
        if t.get("recurrence", "once") == "once":
            t["state"] = "done"

    store["tasks"] = remaining_tasks + due_tasks
    _save_store(store)

    sys.stdout.write("\n".join(output_lines).strip())
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
