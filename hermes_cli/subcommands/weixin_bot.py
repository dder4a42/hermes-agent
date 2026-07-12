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
        "--clone-config",
        dest="clone",
        action="store_true",
        help="Clone config/.env/skills from the active profile when creating the profile",
    )

    # ── Secret-carrying flags. Values are never echoed. ─────────────────────
    create.add_argument(
        "--weixin-token",
        default=None,
        help="iLink Bot token to write into the profile's .env (never echoed).",
    )
    create.add_argument(
        "--weixin-account-id",
        default=None,
        help="iLink Bot account id to write into the profile's .env.",
    )
    create.add_argument(
        "--allowed-user",
        action="append",
        default=None,
        metavar="WEIXIN_USER_ID",
        help=(
            "Weixin user id permitted to interact with this bot. Repeatable — "
            "each --allowed-user is appended to the comma-separated "
            "WEIXIN_ALLOWED_USERS env var."
        ),
    )
    create.add_argument(
        "--dm-policy",
        choices=("allowlist", "open", "off"),
        default=None,
        help="Set WEIXIN_DM_POLICY. Recommended: 'allowlist' when --allowed-user is given.",
    )
    create.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing .env keys and refresh Research Copilot config "
            "from the source. User JSONL history is always preserved."
        ),
    )

    # ── List subcommand — status view over configured weixin-bot profiles. ─
    list_cmd = command_subparsers.add_parser(
        "list",
        help="List Weixin bot profiles and their configuration state",
    )
    list_cmd.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Output format (default: table).",
    )

    parser.set_defaults(func=cmd_weixin_bot)
