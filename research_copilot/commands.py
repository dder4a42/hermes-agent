"""Command handlers for Research Copilot slash/CLI commands."""

from __future__ import annotations

from datetime import datetime, timezone


def _open_library():
    from .runtime import open_library, runtime_paths

    return open_library(runtime_paths()["database"])


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
        "/paper now\n"
        "/paper ask <question>\n"
        "/paper discuss [<paper_id>] [<question>]\n"
        "/paper end"
    )


def _format_topics() -> str:
    from .runtime import load_yaml, runtime_paths

    try:
        data = load_yaml(runtime_paths()["topics"])
    except (FileNotFoundError, ValueError) as exc:
        return f"Research Copilot Topics\n\n(unavailable: {exc})"
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
    connection, _repository = _open_library()
    try:
        rows = connection.execute(
            """
            SELECT recommendations.id AS recommendation_id,
                   recommendations.item_id, recommendations.recommended_at,
                   recommendations.score, research_items.title
            FROM recommendations
            JOIN research_items ON research_items.id = recommendations.item_id
            ORDER BY recommendations.recommended_at DESC, recommendations.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        recs = []
        for row in rows:
            topics = [
                value["topic_id"] for value in connection.execute(
                    "SELECT topic_id FROM item_topics WHERE item_id=? "
                    "ORDER BY confidence DESC, topic_id",
                    (row["item_id"],),
                )
            ]
            recs.append({**dict(row), "topic_matches": topics})
    finally:
        connection.close()
    if not recs:
        return "Recent Research Picks\n\n(no recommendations yet)"
    lines = ["Recent Research Picks"]
    for idx, rec in enumerate(recs, start=1):
        rid = rec.get("item_id", "?")
        title = rec.get("title", "Untitled")
        matches = rec.get("topic_matches") or []
        if isinstance(matches, list):
            match_text = ", ".join(str(m) for m in matches)
        else:
            match_text = str(matches)
        lines.append("")
        lines.append(f"{idx}. {rid}")
        lines.append(str(title))
        lines.append(f"Score: {float(rec.get('score') or 0):.4f}")
        if match_text:
            lines.append(f"Matches: {match_text}")
    return "\n".join(lines)


def _record_action(action: str, item_id: str, **extra) -> str:
    item_id = item_id.strip()
    if not item_id:
        return f"Usage: /paper {action} <id>"
    connection, repository = _open_library()
    try:
        rec = connection.execute(
            "SELECT 1 FROM recommendations WHERE item_id=?", (item_id,)
        ).fetchone()
        if rec is None:
            return f"Recommendation {item_id} not found. Use /paper history to see recent ids."
        kind = "note" if action == "feedback" else action
        payload = {k: v for k, v in extra.items() if v not in (None, "")}
        repository.record_feedback(
            item_id, kind=kind, created_at=datetime.now(timezone.utc), payload=payload,
        )
    finally:
        connection.close()
    verb = {
        "save": "Saved",
        "skip": "Skipped",
        "read": "Marked read",
        "feedback": "Recorded feedback for",
    }.get(action, action)
    return f"{verb} {item_id}."


def _format_health() -> str:
    """Return SourceRun-based Research Library health."""
    from .health import build_health_report, render_health_report

    connection, _repository = _open_library()
    try:
        return render_health_report(build_health_report(connection)).rstrip()
    finally:
        connection.close()


def _trigger_daily_pick() -> str:
    """Trigger the profile-local Library recommender on the next cron tick."""
    from hermes_constants import get_hermes_home
    from cron.jobs import AmbiguousJobReference, trigger_job, use_cron_store

    with use_cron_store(get_hermes_home()):
        job = None
        selected_name = "research-library-recommend"
        for candidate in (selected_name, "daily-paper-pick"):
            try:
                job = trigger_job(candidate)
            except AmbiguousJobReference as exc:
                matches = ", ".join(j.get("id", "?") for j in exc.matches)
                return (
                    f"Multiple {candidate} cron jobs match this profile. "
                    f"Use hermes cron run <job_id> with one of: {matches}"
                )
            if job:
                selected_name = candidate
                break
    if not job:
        return (
            "Research Library recommendation cron job was not found in this Hermes profile.\n"
            "Create it with hermes cron create, or initialize this profile's Research Copilot cron jobs."
        )
    return (
        f"Triggered {selected_name} ({job.get('id')}).\n"
        "It will run on the next cron scheduler tick and deliver through its configured target."
    )


def handle_paper_command(args: str = "") -> str:
    """Handle a Research Copilot command and return user-facing text."""
    args = (args or "").strip()
    if not args:
        return _usage()
    # Route ask / discuss / end through the shared domain-discussion module.
    parts_first = args.split(maxsplit=1)
    first_sub = parts_first[0].lower() if parts_first else ""
    if first_sub in {"ask", "discuss", "end"}:
        try:
            from gateway.domain_discussion import handle_domain_subcommand
        except ImportError:
            return "❌ discussion module unavailable"
        response = handle_domain_subcommand("paper", args)
        if response is not None:
            return response
    parts = args.split()
    subcmd = parts[0].lower()
    rest = parts[1:]

    if subcmd == "topics":
        return _format_topics()
    if subcmd == "history":
        return _format_history(rest)
    if subcmd == "save":
        return _record_action("save", rest[0] if rest else "")
    if subcmd == "read":
        return _record_action("read", rest[0] if rest else "")
    if subcmd == "skip":
        item_id = rest[0] if rest else ""
        reason = " ".join(rest[1:]).strip()
        return _record_action("skip", item_id, reason=reason)
    if subcmd == "feedback":
        item_id = rest[0] if rest else ""
        text = " ".join(rest[1:]).strip()
        if item_id and not text:
            return "Usage: /paper feedback <id> <text>"
        return _record_action("feedback", item_id, text=text)
    if subcmd == "health":
        return _format_health()
    if subcmd == "now":
        return _trigger_daily_pick()
    return _usage()
