#!/usr/bin/env python3
"""Task Surfacer -- checks every 5 minutes for due tasks.

Reads $HERMES_HOME/thoughts.json, finds active tasks whose absolute
scheduled_at has arrived (or whose pre-reminder window has opened), and
prints reminders to stdout for cron delivery.

Two firing paths per task:
  * pre-reminder: when now >= scheduled_at - remind_before_min AND
    lead_reminded_at is None. Renders pending checklist items so the user
    can prep. Fires at most once.
  * main reminder: when now >= scheduled_at AND last_reminded is None
    (single-fire semantics). Renders url / location / attendees /
    outstanding checklist alongside the title.

Recurring tasks (recurrence != 'once') advance to the next occurrence
after the main reminder fires; one-shot tasks flip state='done'.

Designed to run as a no_agent=True cron job. Silent when nothing is due.

Environment:
  HERMES_HOME  -- profile home; defaults to ~/.hermes.
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
    """Legacy cron matcher for tasks that only have schedule_cron."""
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


def _parse_iso(dt_str):
    if not isinstance(dt_str, str) or not dt_str.strip():
        return None
    try:
        return datetime.fromisoformat(dt_str.strip().replace("Z", "+00:00"))
    except Exception:
        return None


def _now_local(tz_hint=None) -> datetime:
    if tz_hint is not None and tz_hint.tzinfo is not None:
        return datetime.now(tz_hint.tzinfo)
    return datetime.now(timezone(timedelta(hours=8)))


def _advance_recurrence(scheduled: datetime, recurrence: str):
    r = (recurrence or "once").lower()
    if r == "daily":
        return scheduled + timedelta(days=1)
    if r == "weekly":
        return scheduled + timedelta(days=7)
    if r == "monthly":
        month = scheduled.month + 1
        year = scheduled.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        try:
            return scheduled.replace(year=year, month=month)
        except ValueError:
            import calendar
            last = calendar.monthrange(year, month)[1]
            return scheduled.replace(year=year, month=month, day=last)
    if r == "yearly":
        try:
            return scheduled.replace(year=scheduled.year + 1)
        except ValueError:
            return scheduled + timedelta(days=365)
    return None


def _pending_checklist(task: dict):
    return [it for it in (task.get("checklist") or []) if not it.get("done")]


def _fmt_main(task: dict) -> str:
    title = task.get("title", "Untitled")
    narrated = _try_narrate(task, mode="main")
    lead = narrated or f"⏰ Reminder: {title}"
    lines = [f"⏰ {lead}"] if narrated else [lead]
    notes = task.get("notes")
    if notes:
        lines.append(f"📝 {notes}")
    location = task.get("location")
    if location:
        lines.append(f"📍 {location}")
    url = task.get("url")
    if url:
        lines.append(f"🔗 {url}")
    attendees = task.get("attendees") or []
    if attendees:
        lines.append(f"👥 {', '.join(attendees)}")
    pending = _pending_checklist(task)
    if pending:
        lines.append("📋 Pending:")
        for it in pending:
            lines.append(f"  [ ] {it.get('text','')}")
    return "\n".join(lines) + "\n"


def _fmt_pre(task: dict, minutes: int) -> str:
    pending = _pending_checklist(task)
    title = task.get("title", "Untitled")
    narrated = _try_narrate(task, mode="pre", minutes_left=minutes)
    lead = narrated or f"距 {title} 还有 {minutes} 分钟"
    lines = [f"⏰ {lead}"]
    if pending:
        lines.append("📋 尚未完成的准备：")
        for it in pending:
            lines.append(f"  [ ] {it.get('text','')}")
    location = task.get("location")
    if location:
        lines.append(f"📍 {location}")
    url = task.get("url")
    if url:
        lines.append(f"🔗 {url}")
    return "\n".join(lines) + "\n"


def _try_narrate(task: dict, *, mode: str, minutes_left: int = 0):
    """Ask the LLM narrator for a single-line opener. Returns None on
    any failure (missing key, network, empty output) so the caller falls
    back to the static template."""
    try:
        # Optional import — surfacer must still work if the module is
        # missing or hermes-agent isn't on the path (e.g. bare cron env
        # before installation completes).
        import sys, os
        from pathlib import Path
        repo_root = Path(__file__).resolve().parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from tools.reminder_narrator import narrate_reminder
    except Exception:
        return None
    try:
        return narrate_reminder(task, mode=mode, minutes_left=minutes_left)
    except Exception:
        return None


def main() -> None:
    path = _store_path()
    if not path.exists():
        return
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if store.get("schema_version") != 1:
        return

    now = _now_local()
    now_utc = datetime.now(timezone.utc)
    output_lines = []
    changed = False

    for t in store.get("tasks", []):
        if t.get("state") != "active":
            continue

        scheduled = _parse_iso(t.get("scheduled_at"))
        remind_before = int(t.get("remind_before_min") or 0)
        already_lead = t.get("lead_reminded_at") is not None
        already_main = t.get("last_reminded") is not None

        fired_main = False
        fired_pre = False

        if scheduled is not None:
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=now.tzinfo)

            # Pre-reminder window: [scheduled - remind_before, scheduled).
            if remind_before > 0 and not already_lead and not already_main:
                lead_at = scheduled - timedelta(minutes=remind_before)
                if lead_at <= now < scheduled:
                    fired_pre = True

            # Main reminder: now >= scheduled and not yet fired.
            if not already_main and now >= scheduled:
                fired_main = True
        # schema_version=1 requires an absolute scheduled_at. Recurrence is
        # advanced from that timestamp after firing; there is no legacy cron
        # fallback to create a second source of scheduling truth.

        if fired_pre and not fired_main:
            minutes_left = int((scheduled - now).total_seconds() // 60) if scheduled else remind_before
            minutes_left = max(0, minutes_left)
            output_lines.append(_fmt_pre(t, minutes_left))
            t["lead_reminded_at"] = now_utc.isoformat()
            changed = True
            continue

        if not fired_main:
            continue

        output_lines.append(_fmt_main(t))
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
                # Reset per-occurrence gates so the next cycle fires.
                t["last_reminded"] = None
                t["lead_reminded_at"] = None

    if changed:
        _save_store(store)

    if output_lines:
        sys.stdout.write("\n".join(output_lines).strip())
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
