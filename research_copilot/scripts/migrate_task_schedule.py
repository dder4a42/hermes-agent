#!/usr/bin/env python3
"""Backfill scheduled_at on legacy tasks that only carry schedule_raw.

Reads HERMES_HOME/thoughts.json, finds every task where scheduled_at is
missing or null but schedule_raw is non-empty, and asks the schedule parser
to resolve each one. Runs in dry-run mode by default — pass --apply to
persist the changes.

Environment: same as tools.schedule_parser (DEEPSEEK_API_KEY etc.).

Usage:
  python -m research_copilot.scripts.migrate_task_schedule           # dry-run
  python -m research_copilot.scripts.migrate_task_schedule --apply   # write
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def _store_path() -> Path:
    return _hermes_home() / "thoughts.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Persist the resolved scheduled_at fields.")
    args = parser.parse_args()

    from tools.schedule_parser import parse_schedule

    path = _store_path()
    if not path.exists():
        print(f"no store at {path}")
        return 0
    store = json.loads(path.read_text(encoding="utf-8"))

    resolved = 0
    unresolved = 0
    for t in store.get("tasks", []):
        if t.get("scheduled_at"):
            continue  # already resolved
        raw = (t.get("schedule_raw") or "").strip()
        if not raw:
            continue

        result = parse_schedule(raw)
        print(f"tk={t.get('id')}  raw={raw!r:<30}  ok={result.ok}  "
              f"at={result.scheduled_at}  rec={result.recurrence}  "
              f"conf={result.confidence:.2f}")
        if not result.ok:
            print(f"    err: {result.error}")
            unresolved += 1
            if args.apply:
                t.setdefault("parse", {}).update({
                    "error": result.error,
                    "confidence": None,
                    "reasoning": None,
                })
            continue

        resolved += 1
        if args.apply:
            t["scheduled_at"] = result.scheduled_at
            t["schedule_cron"] = result.schedule_cron or ""
            if (t.get("recurrence") or "once") == "once":
                t["recurrence"] = result.recurrence
            t.setdefault("parse", {}).update({
                "error": None,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
            })
            # Backfill fields the new schema expects.
            t.setdefault("lead_reminded_at", None)

    print(f"\n{resolved} resolved, {unresolved} unresolved")

    if args.apply and resolved > 0:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        print(f"wrote {path}")
    else:
        print("(dry-run — no changes written; pass --apply to persist)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
