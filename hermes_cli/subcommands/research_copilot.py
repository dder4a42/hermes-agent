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
    for action in ("enable", "disable"):
        source_toggle = sources_sub.add_parser(
            action, help=f"{action.title()} one source in sources.yaml",
        )
        source_toggle.add_argument("source_id", help="Canonical source id")
    source_option = sources_sub.add_parser(
        "set-option", help="Set one provider option using a YAML scalar value",
    )
    source_option.add_argument("source_id", help="Canonical source id")
    source_option.add_argument("key", help="Provider option key")
    source_option.add_argument("value", help="YAML scalar/list/mapping value")
    source_budget = sources_sub.add_parser(
        "set-budget", help="Set max_requests, max_items, or max_items_per_topic",
    )
    source_budget.add_argument("source_id", help="Canonical source id")
    source_budget.add_argument(
        "key", choices=("max_requests", "max_items", "max_items_per_topic"),
    )
    source_budget.add_argument("value", type=int)

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

    config = command_subparsers.add_parser(
        "config", help="Validate or migrate Topic/Profile configuration",
    )
    config_sub = config.add_subparsers(dest="research_config_command")
    config_sub.add_parser("validate", help="Validate cross-file Topic/Profile references")
    migrate_config = config_sub.add_parser(
        "migrate", help="Preview or apply the schema-v2 Topic/Profile migration",
    )
    migrate_config.add_argument(
        "--apply", action="store_true", help="Apply after validation and timestamped backup",
    )

    collect = command_subparsers.add_parser("collect", help="Collect research items into the Library")
    collect.add_argument("--source", default=None, help="Run one source by id")
    collect.add_argument("--dry-run", action="store_true", help="Fetch and classify without writing state")
    collect.add_argument(
        "--show-items", action="store_true",
        help="Show bounded candidate titles and URLs in dry-run output",
    )
    collect.add_argument(
        "--max-requests", type=int, default=None,
        help="Override the global request ceiling for this run",
    )
    collect.add_argument(
        "--max-new-items", type=int, default=None,
        help="Override the global new-item ceiling for this run",
    )

    scout = command_subparsers.add_parser("scout", help="Run low-frequency Codex web discovery")
    scout.add_argument("--dry-run", action="store_true", help="Print validated discoveries without saving staging output")
    scout.add_argument(
        "--timeout", type=int, default=None,
        help="One-run timeout override (default: research_copilot.scout.timeout_seconds)",
    )
    scout.add_argument(
        "--promote", metavar="ARTIFACT", default=None,
        help="Promote explicitly selected candidates from a saved Scout JSON artifact",
    )
    scout.add_argument(
        "--candidate", type=int, action="append", default=None, metavar="N",
        help="1-based candidate index to promote; repeat for multiple candidates",
    )

    command_subparsers.add_parser("health", help="Show SourceRun-based health")
    command_subparsers.add_parser("doctor", help="Show code, config and database provenance")
    duplicates = command_subparsers.add_parser(
        "duplicates", help="Inspect or explicitly merge duplicate Library identities",
    )
    duplicates.add_argument("--merge", metavar="SOURCE_ITEM_ID")
    duplicates.add_argument("--into", metavar="TARGET_ITEM_ID")
    duplicates.add_argument("--reason", default="")
    duplicates.add_argument("--apply", action="store_true")

    wiki = command_subparsers.add_parser("wiki", help="Configure, export, and lint the LLM Wiki")
    wiki_sub = wiki.add_subparsers(dest="research_wiki_command")
    wiki_configure = wiki_sub.add_parser("configure", help="Persist the Obsidian vault location")
    wiki_configure.add_argument("--vault", required=True, help="Obsidian vault directory")
    wiki_configure.add_argument(
        "--library-subdir", default="Research Library",
        help="Wiki directory inside the vault (default: Research Library)",
    )
    wiki_export = wiki_sub.add_parser("export", help="Export the Library and curated wiki layers")
    wiki_export.add_argument("--vault", default=None, help="One-run vault override")
    wiki_export.add_argument("--full", action="store_true", help="Refresh every generated note")
    wiki_lint = wiki_sub.add_parser("lint", help="Check links, metadata, duplicates, and coverage")
    wiki_lint.add_argument("--vault", default=None, help="One-run vault override")
    wiki_lint.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    wiki_sub.add_parser(
        "publish-check", help="Check Wiki lint and Git publication safety gates",
    )
    wiki_reconcile = wiki_sub.add_parser(
        "reconcile", help="Reconcile substantial Wiki analyses into Library state",
    )
    wiki_reconcile.add_argument(
        "--apply", action="store_true", help="Persist deep_researched promotions",
    )

    recommend = command_subparsers.add_parser("recommend", help="Rank and recommend the top Library item")
    recommend.add_argument("--dry-run", action="store_true", help="Rank without writing a recommendation")
    recommend.add_argument("--threshold", type=float, default=0.72, help="Minimum score (default: 0.72)")
    recommend.add_argument("--delivery-context", action="store_true", help=argparse.SUPPRESS)
    triage = command_subparsers.add_parser(
        "triage", help="Render a bounded, profile-aware candidate queue",
    )
    triage.add_argument("--limit", type=int, default=None, help="One-run candidate limit")
    triage.add_argument("--threshold", type=float, default=0.0, help="Minimum score (default: 0)")
    triage.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    enrich = command_subparsers.add_parser("enrich-newsletters", help="Resolve and inspect staged newsletter links")
    enrich.add_argument("--dry-run", action="store_true", help="Resolve without updating Library state")
    enrich.add_argument("--limit", type=int, default=50, help="Maximum staged entries (default: 50)")
    promote = command_subparsers.add_parser("promote-newsletters", help="Promote enriched newsletter entries into the Library")
    promote.add_argument("--dry-run", action="store_true", help="Classify without updating Library state")
    promote.add_argument("--limit", type=int, default=50, help="Maximum staged entries (default: 50)")
    daily = command_subparsers.add_parser("daily-report", help="Render a grouped Research Library daily report")
    daily.add_argument("--days", type=int, default=1, help="Lookback window (default: 1 day)")

    deep_research = command_subparsers.add_parser(
        "deep-research", help="Validate and import structured deep-research artifacts",
    )
    deep_research_sub = deep_research.add_subparsers(dest="research_deep_research_command")
    deep_research_import = deep_research_sub.add_parser(
        "import", help="Validate an artifact and optionally import it",
    )
    deep_research_import.add_argument("path", help="YAML or JSON artifact path")
    deep_research_import.add_argument(
        "--apply", action="store_true", help="Persist after validation (default: preview)",
    )
    deep_research_run = deep_research_sub.add_parser(
        "run", help="Run one isolated web-only research task and import its artifact",
    )
    deep_research_run.add_argument(
        "--item-id", default=None, help="Research an eligible recommended item by exact id",
    )
    deep_research_run.add_argument(
        "--dry-run", action="store_true", help="Show the selected item and boundaries only",
    )
    deep_research_run.add_argument(
        "--timeout", type=int, default=None, help="Override the configured timeout in seconds",
    )
    deep_research_run.add_argument(
        "--no-export", action="store_true", help="Do not compile the updated Wiki after import",
    )
    deep_research_run.add_argument(
        "--delivery-context", action="store_true", help=argparse.SUPPRESS,
    )

    evidence = command_subparsers.add_parser(
        "evidence", help="Create and review profile-linked evidence records",
    )
    evidence_sub = evidence.add_subparsers(dest="research_evidence_command")
    evidence_list = evidence_sub.add_parser("list", aliases=["ls"], help="List evidence records")
    evidence_list.add_argument(
        "--status", choices=("pending", "accepted", "rejected"), default=None,
    )
    evidence_add = evidence_sub.add_parser("add", help="Add pending evidence for human review")
    evidence_add.add_argument("item_id")
    evidence_add.add_argument("--belief", default=None, help="Referenced belief id")
    evidence_add.add_argument("--prompt", default=None, help="Referenced question/gap id")
    evidence_add.add_argument(
        "--relation", required=True,
        choices=("supports", "challenges", "contextualizes"),
    )
    evidence_add.add_argument(
        "--claim-type", required=True,
        choices=("source_claim", "agent_inference", "personal_take"),
    )
    evidence_add.add_argument("--strength", required=True, type=float)
    evidence_add.add_argument(
        "--source-quality", default="unknown",
        choices=("primary", "official", "secondary", "community", "unknown"),
    )
    evidence_add.add_argument("--claim", required=True)
    evidence_add.add_argument("--rationale", default="")
    evidence_review = evidence_sub.add_parser("review", help="Accept or reject pending evidence")
    evidence_review.add_argument("evidence_id")
    decision = evidence_review.add_mutually_exclusive_group(required=True)
    decision.add_argument("--accept", action="store_true")
    decision.add_argument("--reject", action="store_true")
    evidence_review.add_argument("--note", default="")

    profile_proposal = command_subparsers.add_parser(
        "profile-proposal", help="Build a non-mutating profile update proposal",
    )
    profile_proposal.add_argument(
        "--dry-run", action="store_true", help="Print YAML without writing a proposal file",
    )
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
