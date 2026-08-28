"""Guarded Git publication for the profile-configured Research Wiki vault."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .wiki import audit_wiki, resolve_wiki_root


@dataclass(frozen=True)
class VaultPublishCheck:
    allowed: bool
    vault: Path
    changed: int
    deleted: int
    untracked: int
    reasons: tuple[str, ...] = ()


def _git(vault: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=vault, check=check, capture_output=True,
        text=True, timeout=120,
    )


def _settings() -> dict[str, Any]:
    from hermes_cli.config import load_config_readonly

    root = load_config_readonly().get("research_copilot", {})
    wiki = root.get("wiki", {}) if isinstance(root, dict) else {}
    sync = wiki.get("git_sync", {}) if isinstance(wiki, dict) else {}
    if not isinstance(sync, dict):
        sync = {}
    prefixes = sync.get("forbidden_prefixes", [".hermes-archive/"])
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    if not isinstance(prefixes, list) or not all(isinstance(value, str) for value in prefixes):
        raise ValueError("research_copilot.wiki.git_sync.forbidden_prefixes must be a list of strings")
    return {
        "enabled": sync.get("enabled") is True,
        "remote": str(sync.get("remote") or "origin"),
        "branch": str(sync.get("branch") or "main"),
        "max_changed_files": int(sync.get("max_changed_files", 75)),
        "max_deleted_files": int(sync.get("max_deleted_files", 0)),
        "max_untracked_files": int(sync.get("max_untracked_files", 25)),
        "forbidden_prefixes": tuple(prefixes),
    }


def _status(vault: Path) -> list[tuple[str, str]]:
    output = _git(vault, "status", "--porcelain=v1", "--untracked-files=all").stdout
    rows: list[tuple[str, str]] = []
    for line in output.splitlines():
        if len(line) >= 4:
            rows.append((line[:2], line[3:]))
    return rows


def check_vault_publication() -> VaultPublishCheck:
    wiki_root, _subdir, vault = resolve_wiki_root()
    settings = _settings()
    reasons: list[str] = []
    if not settings["enabled"]:
        reasons.append("git sync is disabled in config.yaml")
    if not (vault / ".git").exists():
        reasons.append(f"vault is not a Git repository: {vault}")

    audit = audit_wiki(wiki_root)
    if audit.errors:
        reasons.append(f"Wiki lint has {audit.errors} error(s)")

    rows = _status(vault) if (vault / ".git").exists() else []
    deleted = sum("D" in code for code, _path in rows)
    untracked = sum(code == "??" for code, _path in rows)
    changed = len(rows)
    forbidden = sorted({
        path for _code, path in rows
        if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in settings["forbidden_prefixes"])
    })
    if forbidden:
        reasons.append("forbidden paths are dirty: " + ", ".join(forbidden[:5]))
    if changed > settings["max_changed_files"]:
        reasons.append(
            f"changed files {changed} exceed limit {settings['max_changed_files']}"
        )
    if deleted > settings["max_deleted_files"]:
        reasons.append(
            f"deleted files {deleted} exceed limit {settings['max_deleted_files']}"
        )
    if untracked > settings["max_untracked_files"]:
        reasons.append(
            f"untracked files {untracked} exceed limit {settings['max_untracked_files']}"
        )
    return VaultPublishCheck(
        allowed=not reasons, vault=vault, changed=changed,
        deleted=deleted, untracked=untracked, reasons=tuple(reasons),
    )


def render_publish_check(result: VaultPublishCheck) -> str:
    state = "allowed" if result.allowed else "blocked"
    lines = [
        f"Vault publication {state}: changed={result.changed} "
        f"deleted={result.deleted} untracked={result.untracked}",
        f"  {result.vault}",
    ]
    lines.extend(f"  - {reason}" for reason in result.reasons)
    return "\n".join(lines)


def sync_vault() -> tuple[bool, str]:
    """Commit and push only after lint, change-volume and divergence gates pass."""
    result = check_vault_publication()
    if not result.allowed:
        return False, render_publish_check(result)
    if result.changed == 0:
        return True, ""

    settings = _settings()
    remote = settings["remote"]
    branch = settings["branch"]
    try:
        _git(result.vault, "fetch", "--quiet", remote, branch)
        divergence = _git(
            result.vault, "rev-list", "--left-right", "--count",
            f"HEAD...{remote}/{branch}",
        ).stdout.split()
        remote_ahead = int(divergence[1]) if len(divergence) == 2 else 0
        if remote_ahead:
            return False, f"Vault publication blocked: {remote}/{branch} is ahead by {remote_ahead} commit(s)"
        _git(result.vault, "add", "-A")
        _git(
            result.vault, "commit", "-m",
            f"sync: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        )
        _git(result.vault, "push", remote, f"HEAD:{branch}")
        short = _git(result.vault, "rev-parse", "--short", "HEAD").stdout.strip()
        return True, f"vault synced: {short}"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, f"vault sync FAILED: {exc}"
