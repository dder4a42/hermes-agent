"""Command handlers for Research Copilot slash/CLI commands."""

from __future__ import annotations

from datetime import datetime, timezone

from .storage import (
    append_interaction,
    find_recommendation,
    load_config,
    load_topics,
    read_recommendations,
    update_candidate_status,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _usage() -> str:
    return (
        "Research Copilot\n\n"
        "Commands:\n"
        "/paper topics\n"
        "/paper history [n]\n"
        "/paper save <id>\n"
        "/paper skip <id> [reason]\n"
        "/paper read <id>\n"
        "/paper feedback <id> <text>\n"
        "/paper health\n"
        "/paper now"
    )


def _format_topics() -> str:
    data = load_topics()
    topics = data.get("topics") if isinstance(data, dict) else []
    if not topics:
        return "Research Copilot Topics\n\n(no topics configured)"

    def _sort_key(topic: dict) -> tuple[int, float, str]:
        active_rank = 0 if topic.get("status", "active") == "active" else 1
        try:
            priority = float(topic.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0.0
        return (active_rank, -priority, str(topic.get("id") or ""))

    lines = ["Research Copilot Topics"]
    for topic in sorted((t for t in topics if isinstance(t, dict)), key=_sort_key):
        tid = topic.get("id", "?")
        name = topic.get("name", tid)
        status = topic.get("status", "active")
        priority = topic.get("priority", "?")
        lines.append(f"- {tid}: {name} [{status}, priority={priority}]")
    return "\n".join(lines)


def _format_history(args: list[str]) -> str:
    limit = 5
    if args:
        try:
            limit = max(1, min(20, int(args[0])))
        except ValueError:
            return "Usage: /paper history [n]"
    recs = read_recommendations(limit=limit)
    if not recs:
        return "Recent Research Picks\n\n(no recommendations yet)"
    lines = ["Recent Research Picks"]
    for idx, rec in enumerate(recs, start=1):
        rid = rec.get("id", "?")
        title = rec.get("title", "Untitled")
        matches = rec.get("topic_matches") or []
        if isinstance(matches, list):
            match_text = ", ".join(str(m) for m in matches)
        else:
            match_text = str(matches)
        lines.append("")
        lines.append(f"{idx}. {rid}")
        lines.append(str(title))
        if match_text:
            lines.append(f"Matches: {match_text}")
    return "\n".join(lines)


def _record_action(action: str, item_id: str, *, status: str | None = None, **extra) -> str:
    item_id = item_id.strip()
    if not item_id:
        return f"Usage: /paper {action} <id>"
    rec = find_recommendation(item_id)
    if rec is None:
        return f"Recommendation {item_id} not found. Use /paper history to see recent ids."
    record = {
        "type": action,
        "item_id": item_id,
        "title": rec.get("title"),
        "created_at": _now_iso(),
    }
    record.update({k: v for k, v in extra.items() if v not in (None, "")})
    append_interaction(record)
    if status:
        update_candidate_status(item_id, status)
    verb = {
        "save": "Saved",
        "skip": "Skipped",
        "read": "Marked read",
        "feedback": "Recorded feedback for",
    }.get(action, action)
    return f"{verb} {item_id}."


def _trigger_daily_pick() -> str:
    """Trigger the profile-local daily-paper-pick cron job on the next tick."""
    from hermes_constants import get_hermes_home
    from cron.jobs import AmbiguousJobReference, trigger_job, use_cron_store

    with use_cron_store(get_hermes_home()):
        try:
            job = trigger_job("daily-paper-pick")
        except AmbiguousJobReference as exc:
            matches = ", ".join(j.get("id", "?") for j in exc.matches)
            return (
                "Multiple daily-paper-pick cron jobs match this profile. "
                f"Use hermes cron run <job_id> with one of: {matches}"
            )
    if not job:
        return (
            "daily-paper-pick cron job was not found in this Hermes profile.\n"
            "Create it with hermes cron create, or initialize this profile's Research Copilot cron jobs."
        )
    return (
        f"Triggered daily-paper-pick ({job.get('id')}).\n"
        "It will run on the next cron scheduler tick and deliver through its configured target."
    )


def handle_paper_command(args: str = "") -> str:
    """Handle a Research Copilot command and return user-facing text."""
    args = (args or "").strip()
    if not args:
        return _usage()
    parts = args.split()
    subcmd = parts[0].lower()
    rest = parts[1:]

    if subcmd == "topics":
        return _format_topics()
    if subcmd == "history":
        return _format_history(rest)
    if subcmd == "save":
        return _record_action("save", rest[0] if rest else "", status="saved")
    if subcmd == "read":
        return _record_action("read", rest[0] if rest else "", status="read")
    if subcmd == "skip":
        item_id = rest[0] if rest else ""
        reason = " ".join(rest[1:]).strip()
        return _record_action("skip", item_id, status="skipped", reason=reason)
    if subcmd == "feedback":
        item_id = rest[0] if rest else ""
        text = " ".join(rest[1:]).strip()
        if item_id and not text:
            return "Usage: /paper feedback <id> <text>"
        return _record_action("feedback", item_id, text=text)
    if subcmd == "health":
        cfg = load_config()
        pipeline = cfg.get("pipeline", "unknown") if isinstance(cfg, dict) else "unknown"
        return f"Research Copilot Health\nPipeline: {pipeline}\nUse /paper history to inspect recent picks."
    if subcmd == "now":
        return _trigger_daily_pick()
    return _usage()
