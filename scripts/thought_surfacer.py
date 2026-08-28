#!/usr/bin/env python3
"""Thought incubator — surfaces one due idea with a useful next step.

Reads ``$HERMES_HOME/thoughts.json`` (defaults to ``~/.hermes/thoughts.json``),
selects an active thought (prioritising oldest that hasn't been surfaced
recently), prints it to stdout for cron delivery (no_agent=True).

Output format:
  💭 Thought Incubation
  <title>
  <summary>

Silent when no active thoughts exist.

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


def _parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _settings() -> dict:
    defaults = {
        "active_hours": {"start": "09:00", "end": "21:30"},
        "max_nudges_per_day": 2,
        "unanswered_backoff": ["3d", "7d", "30d"],
    }
    path = _hermes_home() / "config.yaml"
    if not path.exists():
        return defaults
    try:
        import yaml
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        custom = ((cfg.get("thoughts") or {}).get("incubation") or {})
        return {**defaults, **custom}
    except Exception:
        return defaults


def _inside_active_hours(settings: dict, now: datetime) -> bool:
    hours = settings.get("active_hours") or {}
    try:
        start_h, start_m = map(int, str(hours.get("start", "09:00")).split(":"))
        end_h, end_m = map(int, str(hours.get("end", "21:30")).split(":"))
    except (TypeError, ValueError):
        return True
    minute = now.hour * 60 + now.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    return start <= minute < end if start <= end else minute >= start or minute < end


def _action_line(thought: dict) -> str:
    action = thought.get("next_action") or {}
    kind = action.get("kind") or "clarify"
    prompt = str(action.get("prompt") or "").strip()
    if prompt:
        return prompt
    return {
        "clarify": "这件事现在最需要澄清的一个问题是什么？回复我，我们一起把它说清楚。",
        "research": "要不要先确定一个具体调研问题？你确认后我再开始查资料。",
        "learn": "你想先补哪一块知识？我可以帮你把它变成一个小型学习计划。",
        "track": "这件事最近有新进展、阻碍或方向变化吗？",
        "defer": "这个想法还值得保留吗？你可以回复继续、暂停或归档。",
    }.get(kind, "回复我，我们继续把这个想法推进一步。")


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
    now = datetime.now().astimezone()
    settings = _settings()
    if settings.get("enabled", True) is False or not _inside_active_hours(settings, now):
        return
    today = now.date()
    nudges_today = 0
    for th in store.get("thoughts", []):
        surfaced = _parse_iso(th.get("surfaced_at"))
        if surfaced and surfaced.astimezone().date() == today:
            nudges_today += 1
    if nudges_today >= int(settings.get("max_nudges_per_day") or 2):
        return

    now_utc = datetime.now(timezone.utc)
    active = []
    for th in store.get("thoughts", []):
        if th.get("state") != "active":
            continue
        review = th.get("review") or {}
        due = _parse_iso(review.get("next_review_at"))
        snoozed = _parse_iso(review.get("snoozed_until"))
        if snoozed and snoozed > now_utc:
            continue
        if due and due > now_utc:
            continue
        active.append(th)
    if not active:
        return

    # Pick the thought with the oldest surface time (or never surfaced)
    # that also has the fewest surface_count — fair rotation.
    def _pick_key(th):
        surfaced = th.get("surfaced_at")
        if not surfaced:
            return (0, 0, th.get("created_at", ""))
        count = th.get("surface_count", 0) or 0
        return (1, count, surfaced)

    active.sort(key=_pick_key)
    chosen = active[0]

    title = chosen.get("title", "Untitled idea")
    summary = chosen.get("summary", "")
    source = chosen.get("source", "")

    lines = ["💭 **Thought Incubation**"]
    lines.append(f"**{title}**")
    if summary:
        lines.append("")
        lines.append(summary)
    if source:
        lines.append("")
        lines.append(f"*Origin: {source}*")
    lines.append("")
    lines.append(_action_line(chosen))
    lines.append("")
    lines.append(
        f"Use `/th discuss {chosen.get('id')}` to continue, "
        f"or `/th pause {chosen.get('id')}` / `/th done {chosen.get('id')}`."
    )

    # Mark surfaced
    chosen["surfaced_at"] = now_utc.isoformat()
    chosen["surface_count"] = (chosen.get("surface_count", 0) or 0) + 1
    review = chosen.setdefault("review", {})
    unanswered = int(review.get("unanswered_count") or 0) + 1
    review["unanswered_count"] = unanswered
    backoff = settings.get("unanswered_backoff") or ["3d", "7d", "30d"]
    spec = str(backoff[min(unanswered - 1, len(backoff) - 1)]) if backoff else "30d"
    try:
        days = max(1, int(spec.rstrip("d")))
    except ValueError:
        days = 30
    from datetime import timedelta
    review["next_review_at"] = (now_utc + timedelta(days=days)).isoformat()
    review["snoozed_until"] = None
    chosen["updated_at"] = now_utc.isoformat()
    chosen.setdefault("history", []).append({"at": now_utc.isoformat(), "event": "surfaced"})
    _save_store(store)

    sys.stdout.write("\n".join(lines))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
