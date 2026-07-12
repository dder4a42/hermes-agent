"""CLI handlers for ``hermes research-copilot``."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _source_home_from_args(args: Any) -> Path:
    if getattr(args, "source_home", None):
        return Path(args.source_home).expanduser()

    from hermes_cli.profiles import get_profile_dir, normalize_profile_name

    source = normalize_profile_name(getattr(args, "source", None) or "default")
    return get_profile_dir(source)


def _init_profile(args: Any) -> int:
    from hermes_cli.profiles import (
        create_profile,
        get_profile_dir,
        normalize_profile_name,
        profile_exists,
        validate_profile_name,
    )
    from research_copilot.bootstrap import (
        initialize_research_copilot_home,
        install_research_copilot_cron,
    )

    profile = normalize_profile_name(args.profile_name)
    validate_profile_name(profile)
    if profile == "default":
        print("Use the default profile directly; init-profile is for named Weixin/user profiles.")
        return 1

    if not profile_exists(profile):
        if not getattr(args, "create_profile", False):
            print(f"Profile '{profile}' does not exist. Re-run with --create-profile or create it first:")
            print(f"  hermes profile create {profile} --clone")
            print(f"  hermes research-copilot init-profile {profile}")
            return 1
        try:
            create_profile(
                profile,
                clone_config=bool(getattr(args, "clone", False)),
                no_alias=True,
            )
        except Exception as exc:  # noqa: BLE001 - user-facing CLI boundary
            print(f"Failed to create profile '{profile}': {exc}")
            return 1

    profile_home = get_profile_dir(profile)
    source_home = _source_home_from_args(args)
    if not source_home.exists():
        print(f"Source HERMES_HOME does not exist: {source_home}")
        return 1

    try:
        init_result = initialize_research_copilot_home(profile_home, source_home=source_home)
        cron_result = install_research_copilot_cron(
            profile_home,
            deliver=getattr(args, "deliver", "weixin") or "weixin",
        )
    except Exception as exc:  # noqa: BLE001 - user-facing CLI boundary
        print(f"Failed to initialize Research Copilot for profile '{profile}': {exc}")
        return 1

    print(f"Research Copilot initialized for profile '{profile}'.")
    print(f"  HERMES_HOME: {profile_home}")
    scripts = [item.removeprefix("scripts/") for item in init_result.get("copied", []) if item.startswith("scripts/")]
    jobs = cron_result.get("created", []) + cron_result.get("existing", [])
    print(f"  Data dir:    {init_result['data_dir']}")
    print(f"  Scripts:     {', '.join(scripts) if scripts else 'none copied'}")
    print(f"  Cron jobs:   {', '.join(jobs)}")
    print()
    print("Next steps:")
    print(f"  1. Configure Weixin credentials in {profile_home}/.env")
    print(f"  2. Start or restart the profile gateway: hermes -p {profile} gateway restart")
    print(f"  3. In Weixin, try: /paper topics")
    return 0


def cmd_research_copilot(args: Any) -> int:
    """Dispatch ``hermes research-copilot`` subcommands."""
    command = getattr(args, "research_copilot_command", None)
    if command == "init-profile":
        return _init_profile(args)
    print("Usage: hermes research-copilot init-profile <profile> [--create-profile]")
    return 1
