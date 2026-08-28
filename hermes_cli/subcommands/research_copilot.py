"""``hermes research-copilot`` subcommand parser."""

from __future__ import annotations

import argparse
from typing import Callable


def build_research_copilot_parser(subparsers, *, cmd_research_copilot: Callable) -> None:
    """Attach the ``research-copilot`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "research-copilot",
        aliases=["research", "rc"],
        help="Initialize and manage Research Copilot profile state",
        description=(
            "Initialize profile-local Research Copilot state, scripts, and cron jobs. "
            "This is the building block for one Weixin bot = one Hermes profile."
        ),
    )
    command_subparsers = parser.add_subparsers(dest="research_copilot_command")

    init_profile = command_subparsers.add_parser(
        "init-profile",
        help="Initialize Research Copilot data and cron jobs for a Hermes profile",
        description=(
            "Prepare a profile-local Research Copilot workspace and install the "
            "paper-fetcher, daily-paper-pick, and paper-health-report cron jobs."
        ),
    )
    init_profile.add_argument("profile_name", help="Target profile name")
    init_profile.add_argument(
        "--source",
        default="default",
        help="Profile to copy existing Research Copilot config/scripts from (default: default)",
    )

    sources = command_subparsers.add_parser("sources", help="Inspect and validate the Source Catalog")
    sources_sub = sources.add_subparsers(dest="research_sources_command")
    sources_sub.add_parser("list", aliases=["ls"], help="List configured sources")
    sources_sub.add_parser("validate", help="Validate source and topic configuration")

    topics = command_subparsers.add_parser("topics", help="Inspect or remove research topics")
    topics_sub = topics.add_subparsers(dest="research_topics_command")
    topics_sub.add_parser("list", aliases=["ls"], help="List configured topics")
    remove_topic = topics_sub.add_parser("remove", help="Remove a topic by id (creates a backup)")
    remove_topic.add_argument("topic_id", help="Topic id to remove")

    profile = command_subparsers.add_parser("profile", help="Inspect or update the research profile")
    profile_sub = profile.add_subparsers(dest="research_profile_command")
    profile_sub.add_parser("show", help="Show long-term research agenda entries")
    remove_agenda = profile_sub.add_parser(
        "remove-agenda", help="Remove a long-term agenda entry by id (creates a backup)",
    )
    remove_agenda.add_argument("agenda_id", help="Agenda id to remove")

    collect = command_subparsers.add_parser("collect", help="Collect research items into the Library")
    collect.add_argument("--source", default=None, help="Run one source by id")
    collect.add_argument("--dry-run", action="store_true", help="Fetch and classify without writing state")

    scout = command_subparsers.add_parser("scout", help="Run low-frequency Codex web discovery")
    scout.add_argument("--dry-run", action="store_true", help="Print validated discoveries without saving staging output")
    scout.add_argument("--timeout", type=int, default=2700, help="Maximum Codex runtime in seconds (default: 2700)")

    command_subparsers.add_parser("health", help="Show SourceRun-based health")
    command_subparsers.add_parser("doctor", help="Show code, config and database provenance")

    recommend = command_subparsers.add_parser("recommend", help="Rank and recommend the top Library item")
    recommend.add_argument("--dry-run", action="store_true", help="Rank without writing a recommendation")
    recommend.add_argument("--threshold", type=float, default=0.72, help="Minimum score (default: 0.72)")
    recommend.add_argument("--delivery-context", action="store_true", help=argparse.SUPPRESS)
    enrich = command_subparsers.add_parser("enrich-newsletters", help="Resolve and inspect staged newsletter links")
    enrich.add_argument("--dry-run", action="store_true", help="Resolve without updating Library state")
    enrich.add_argument("--limit", type=int, default=50, help="Maximum staged entries (default: 50)")
    promote = command_subparsers.add_parser("promote-newsletters", help="Promote enriched newsletter entries into the Library")
    promote.add_argument("--dry-run", action="store_true", help="Classify without updating Library state")
    promote.add_argument("--limit", type=int, default=50, help="Maximum staged entries (default: 50)")
    daily = command_subparsers.add_parser("daily-report", help="Render a grouped Research Library daily report")
    daily.add_argument("--days", type=int, default=1, help="Lookback window (default: 1 day)")
    migrate = command_subparsers.add_parser(
        "migrate-to-library", help="Audit or apply the legacy JSONL migration",
    )
    migrate.add_argument("--dry-run", action="store_true", help="Audit without writing or moving data")
    migrate.add_argument("--apply", action="store_true", help="Apply after backup verification")
    init_profile.add_argument(
        "--source-home",
        default=None,
        help="Explicit source HERMES_HOME path; overrides --source",
    )
    init_profile.add_argument(
        "--deliver",
        default="weixin",
        help="Cron delivery target for installed jobs (default: weixin)",
    )
    init_profile.add_argument(
        "--create-profile",
        action="store_true",
        help="Create the profile directory first when it does not exist",
    )
    init_profile.add_argument(
        "--clone",
        action="store_true",
        help="When creating the profile, clone config/.env/skills from the active profile",
    )

    parser.set_defaults(func=cmd_research_copilot)
