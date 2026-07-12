"""CLI handlers for ``hermes weixin-bot``."""

from __future__ import annotations

import argparse
from typing import Any


def _create(args: Any) -> int:
    from hermes_cli.profiles import normalize_profile_name
    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    profile = normalize_profile_name(args.profile_name)
    init_args = argparse.Namespace(
        research_copilot_command="init-profile",
        profile_name=profile,
        source=getattr(args, "source", "default"),
        source_home=getattr(args, "source_home", None),
        deliver=getattr(args, "deliver", "weixin") or "weixin",
        create_profile=True,
        clone=bool(getattr(args, "clone", False)),
    )
    code = cmd_research_copilot(init_args)
    if code != 0:
        return code

    from hermes_cli.profiles import get_profile_dir

    profile_home = get_profile_dir(profile)
    print()
    print(f"Weixin bot profile '{profile}' is ready.")
    print("Architecture: one user = one Hermes profile = one Weixin bot/account = one gateway process.")
    print("Weixin credentials are not copied automatically; configure them per profile:")
    print(f"  {profile_home}/.env")
    print("Then configure/start the profile gateway:")
    print(f"  hermes -p {profile} gateway setup")
    print(f"  hermes -p {profile} gateway install")
    print(f"  hermes -p {profile} gateway start")
    return 0


def cmd_weixin_bot(args: Any) -> int:
    """Dispatch ``hermes weixin-bot`` subcommands."""
    command = getattr(args, "weixin_bot_command", None)
    if command == "create":
        return _create(args)
    print("Usage: hermes weixin-bot create <profile> [--clone]")
    return 1
