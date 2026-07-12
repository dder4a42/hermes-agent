"""CLI handlers for ``hermes weixin-bot``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _create(args: Any) -> int:
    from hermes_cli.env_writer import EnvKeyConflict, redact_key_names, write_profile_env
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name
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

    profile_home = get_profile_dir(profile)

    env_values: dict[str, str] = {}
    if getattr(args, "weixin_token", None):
        env_values["WEIXIN_TOKEN"] = args.weixin_token
    if getattr(args, "weixin_account_id", None):
        env_values["WEIXIN_ACCOUNT_ID"] = args.weixin_account_id
    allowed = getattr(args, "allowed_user", None) or []
    if allowed:
        env_values["WEIXIN_ALLOWED_USERS"] = ",".join(u.strip() for u in allowed if u.strip())
    if getattr(args, "dm_policy", None):
        env_values["WEIXIN_DM_POLICY"] = args.dm_policy

    written_keys: list[str] = []
    if env_values:
        force = bool(getattr(args, "force", False))
        try:
            _path, written_keys = write_profile_env(
                profile_home,
                env_values,
                mode=0o600,
                overwrite=force,
            )
        except EnvKeyConflict as exc:
            print(
                f"ERROR: {exc}\n"
                "Rerun with --force to overwrite these keys, or remove them from .env first.",
                file=sys.stderr,
            )
            return 2

    print()
    print(f"Weixin bot profile '{profile}' is ready.")
    print("Architecture: one user = one Hermes profile = one Weixin bot/account = one gateway process.")
    if written_keys:
        print(f"Wrote .env keys (values NOT echoed): {redact_key_names(written_keys)}")
        print(f"  → {profile_home}/.env (mode 0600)")
    else:
        print("Weixin credentials are not copied automatically; configure them per profile:")
        print(f"  {profile_home}/.env")
        print("  Or rerun with --weixin-token / --weixin-account-id / --allowed-user.")
    print("Then configure/start the profile gateway:")
    print(f"  hermes -p {profile} gateway setup")
    print(f"  hermes -p {profile} gateway install")
    print(f"  hermes -p {profile} gateway start")
    return 0


def _iso_from_mtime(path: Path) -> str:
    from datetime import datetime, timezone

    if not path.exists():
        return "never"
    ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return ts.isoformat(timespec="seconds")


def _last_recommendation(path: Path) -> str:
    """Return the recommended_at of the most recent recommendation, or 'never'."""
    if not path.exists():
        return "never"
    last: str | None = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = obj.get("recommended_at")
        if ts:
            last = ts
    return last or "never"


def _env_key_present(env_path: Path, key: str) -> bool:
    """True if KEY= appears in env_path with a non-empty value. Never returns
    the value itself."""
    if not env_path.exists():
        return False
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key and v.strip().strip("\"'"):
            return True
    return False


def _cron_names(profile_home: Path) -> set[str]:
    """Read the profile's cron store without leaking config."""
    try:
        from cron.jobs import list_jobs, use_cron_store
    except ImportError:
        return set()
    try:
        with use_cron_store(profile_home):
            return {str(job.get("name") or "") for job in list_jobs(include_disabled=True)}
    except Exception:
        return set()


def _gateway_running(profile_home: Path) -> bool:
    """Best-effort: check for a live gateway_state.json under the profile."""
    state = profile_home / "gateway_state.json"
    if not state.exists():
        return False
    try:
        obj = json.loads(state.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    # gateway_state.json is a dict keyed by platform with a nested "state".
    if not isinstance(obj, dict):
        return False
    for adapter in obj.values():
        if isinstance(adapter, dict) and adapter.get("state") == "connected":
            return True
    return False


def _iter_profiles() -> list[tuple[str, Path]]:
    """Enumerate (name, home) for every discoverable Hermes profile."""
    try:
        from hermes_cli.profiles import list_profiles
    except ImportError:
        return []

    out: list[tuple[str, Path]] = []
    try:
        infos = list_profiles()
    except Exception:
        infos = []
    for info in infos:
        # ProfileInfo has .name and .path.
        name = getattr(info, "name", None)
        path = getattr(info, "path", None)
        if name and path:
            out.append((str(name), Path(path)))
    return out


def _profile_status(name: str, home: Path) -> dict[str, Any]:
    data_dir = home / "research-copilot"
    env_path = home / ".env"
    has_token = _env_key_present(env_path, "WEIXIN_TOKEN")
    has_account = _env_key_present(env_path, "WEIXIN_ACCOUNT_ID")
    rc_initialized = (data_dir / "config.json").exists()
    cron_names = _cron_names(home)
    expected_cron = {"paper-fetcher", "daily-paper-pick", "paper-health-report"}
    return {
        "profile": name,
        "profile_home": str(home),
        "has_creds": bool(has_token and has_account),
        "gateway_running": _gateway_running(home),
        "rc_initialized": rc_initialized,
        "cron_present": expected_cron.issubset(cron_names),
        "last_paper_fetch": _iso_from_mtime(data_dir / "candidates.jsonl"),
        "last_recommendation": _last_recommendation(data_dir / "recommendations.jsonl"),
    }


def _list(args: Any) -> int:
    profiles = _iter_profiles()
    rows = [_profile_status(name, home) for name, home in profiles]

    fmt = getattr(args, "format", "table") or "table"
    if fmt == "json":
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0

    if not rows:
        print("No Hermes profiles discovered.")
        return 0

    headers = ("PROFILE", "CREDS", "GATEWAY", "RC", "CRON", "LAST FETCH", "LAST PICK")
    lines = [headers]
    for r in rows:
        lines.append((
            r["profile"],
            "yes" if r["has_creds"] else "no",
            "up" if r["gateway_running"] else "down",
            "yes" if r["rc_initialized"] else "no",
            "yes" if r["cron_present"] else "no",
            r["last_paper_fetch"],
            r["last_recommendation"],
        ))
    widths = [max(len(row[i]) for row in lines) for i in range(len(headers))]
    for row in lines:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return 0


def cmd_weixin_bot(args: Any) -> int:
    """Dispatch ``hermes weixin-bot`` subcommands."""
    command = getattr(args, "weixin_bot_command", None)
    if command == "create":
        return _create(args)
    if command == "list":
        return _list(args)
    print("Usage: hermes weixin-bot {create|list} ...")
    return 1
