"""``hermes weixin-bot`` subcommand parser."""

from __future__ import annotations

from typing import Callable


def build_weixin_bot_parser(subparsers, *, cmd_weixin_bot: Callable) -> None:
    """Attach the ``weixin-bot`` product bootstrap command."""
    parser = subparsers.add_parser(
        "weixin-bot",
        help="Create one-profile-per-user Weixin bot workspaces",
        description=(
            "Bootstrap the recommended MVP architecture: one user = one Hermes "
            "profile = one Weixin bot/account = one gateway process."
        ),
    )
    command_subparsers = parser.add_subparsers(dest="weixin_bot_command")

    create = command_subparsers.add_parser(
        "create",
        help="Create a profile and initialize Research Copilot for a Weixin user",
    )
    create.add_argument("profile_name", help="Profile/user id to create")
    create.add_argument(
        "--source",
        default="default",
        help="Profile to copy Research Copilot config/scripts from (default: default)",
    )
    create.add_argument(
        "--source-home",
        default=None,
        help="Explicit source HERMES_HOME path; overrides --source",
    )
    create.add_argument(
        "--deliver",
        default="weixin",
        help="Cron delivery target for installed jobs (default: weixin)",
    )
    create.add_argument(
        "--clone",
        action="store_true",
        help="Clone config/.env/skills from the active profile when creating the profile",
    )

    parser.set_defaults(func=cmd_weixin_bot)
