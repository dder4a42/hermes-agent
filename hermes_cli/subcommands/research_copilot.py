"""``hermes research-copilot`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_research_copilot_parser(subparsers, *, cmd_research_copilot: Callable) -> None:
    """Attach the ``research-copilot`` subcommand to ``subparsers``."""
    parser = subparsers.add_parser(
        "research-copilot",
        aliases=["rc"],
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
