#!/usr/bin/env python3
"""One-time migration of the short-lived pre-schema thoughts.json format.

The command is intentionally explicit: it creates a timestamped sibling backup,
writes schema_version=1, validates record counts/IDs, and leaves the backup in
place. It is safe to re-run after migration (it becomes a no-op).
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task(row: dict) -> dict:
    out = dict(row)
    out.setdefault("notes", "")
    out.setdefault("scheduled_at", None)
    out.setdefault("schedule_cron", "")
    out.setdefault("recurrence", "once")
    out.setdefault("state", "active")
    out.setdefault("created_at", _now())
    out.setdefault("last_reminded", None)
    out.setdefault("lead_reminded_at", None)
    out.setdefault("remind_count", 0)
    out.setdefault("input_raw", "")
    out.setdefault("url", "")
    out.setdefault("location", "")
    out.setdefault("attendees", [])
    out.setdefault("tags", [])
    out.setdefault("remind_before_min", 0)
    out.setdefault("checklist", [])
    out.setdefault("parse", {"error": None, "confidence": None, "reasoning": None})
    return out


def _thought(row: dict) -> dict:
    out = dict(row)
    now = _now()
    out.setdefault("summary", "")
    out.setdefault("source", "")
    out.setdefault("tags", [])
    out.setdefault("state", "active")
    out.setdefault("stage", "fuzzy")
    out.setdefault("priority", "normal")
    out.setdefault("created_at", now)
    out.setdefault("updated_at", out["created_at"])
    out.setdefault("surfaced_at", None)
    out.setdefault("surface_count", 0)
    out.setdefault("open_questions", [])
    out.setdefault("next_action", {"kind": "clarify", "prompt": ""})
    out.setdefault("review", {
        "next_review_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "cadence": "7d",
        "snoozed_until": None,
        "unanswered_count": 0,
    })
    out.setdefault("progress", {"status": "unexplored", "last_update_at": None})
    out.setdefault("history", [{"at": out["created_at"], "event": "migrated"}])
    return out


def migrate(path: Path) -> tuple[Path | None, dict]:
    if not path.exists():
        store = {"schema_version": SCHEMA_VERSION, "thoughts": [], "tasks": []}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return None, store

    original = json.loads(path.read_text(encoding="utf-8"))
    if original.get("schema_version") == SCHEMA_VERSION:
        return None, original
    thoughts = [_thought(row) for row in original.get("thoughts", [])]
    tasks = [_task(row) for row in original.get("tasks", [])]
    migrated = {"schema_version": SCHEMA_VERSION, "thoughts": thoughts, "tasks": tasks}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(migrated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)

    check = json.loads(path.read_text(encoding="utf-8"))
    for key in ("thoughts", "tasks"):
        before_ids = [row.get("id") for row in original.get(key, [])]
        after_ids = [row.get("id") for row in check.get(key, [])]
        if before_ids != after_ids:
            shutil.copy2(backup, path)
            raise RuntimeError(f"Migration validation failed for {key}; original restored")
    return backup, check


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=Path.home() / ".hermes" / "thoughts.json")
    args = parser.parse_args()
    backup, store = migrate(args.path.expanduser())
    print(f"Migrated {len(store['tasks'])} tasks and {len(store['thoughts'])} thoughts.")
    print(f"Backup: {backup}" if backup else "Already current; no backup created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
