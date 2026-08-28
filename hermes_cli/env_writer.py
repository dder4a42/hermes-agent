"""Safe ``.env`` writer for profile-local secrets.

``write_profile_env`` merges a dictionary of KEY=VALUE pairs into a profile's
``.env`` file. It parses the existing file if present, refuses to clobber a
key that's already set unless ``overwrite=True``, and writes atomically at
mode 0600 so shell users on the same host can't read secret values.

Never logs values — only the key names are safe to echo. Callers that log
should pass the returned list of key names, not the input dictionary.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable, Mapping


class EnvKeyConflict(RuntimeError):
    """Raised when overwrite=False and one or more keys already exist."""

    def __init__(self, conflicting: Iterable[str]) -> None:
        self.conflicting = tuple(conflicting)
        super().__init__(
            "Refusing to overwrite existing .env keys: "
            + ", ".join(self.conflicting)
        )


def _parse_env(text: str) -> dict[str, str]:
    """Parse a ``.env``-style file. Mirrors hermes_cli.managed_scope._parse_env."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def _needs_quoting(value: str) -> bool:
    """Return True if the value contains characters that require quoting."""
    if value == "":
        return False
    for ch in value:
        if ch.isalnum() or ch in "-_./@:,+":
            continue
        return True
    return False


def _quote(value: str) -> str:
    if not _needs_quoting(value):
        return value
    # Simple double-quote escape — backslashes and inner double-quotes.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_profile_env(
    profile_home: str | Path,
    values: Mapping[str, str],
    *,
    mode: int = 0o600,
    overwrite: bool = False,
) -> tuple[Path, list[str]]:
    """Merge ``values`` into ``<profile_home>/.env``.

    Args:
        profile_home: Path to the profile's HERMES_HOME.
        values: Mapping of KEY -> VALUE pairs to write. Keys with empty/None
            values are skipped (nothing written for that key).
        mode: File permission mode for the resulting .env. Defaults to 0600 so
            other users on the host cannot read secrets.
        overwrite: When False (default), raises ``EnvKeyConflict`` if any
            supplied key already exists in the file.

    Returns ``(path, written_keys)`` where ``written_keys`` is the list of
    key names actually written (excluding any that were empty/None).

    Never logs, prints, or returns the values themselves.
    """
    profile_home = Path(profile_home).expanduser().resolve()
    profile_home.mkdir(parents=True, exist_ok=True)
    env_path = profile_home / ".env"

    existing = _parse_env(env_path.read_text(encoding="utf-8")) if env_path.exists() else {}

    # Filter out empty values — passing "" is a signal from the CLI meaning
    # "the user didn't provide this flag", not "clear the variable".
    to_write = {k: v for k, v in values.items() if v not in (None, "")}

    if not overwrite:
        conflicts = [k for k in to_write if k in existing]
        if conflicts:
            raise EnvKeyConflict(conflicts)

    merged = dict(existing)
    merged.update(to_write)

    # Deterministic key order: keep original file order for pre-existing keys,
    # then append new keys in the order given.
    ordered: list[str] = []
    seen: set[str] = set()
    for k in existing:
        if k in merged:
            ordered.append(k)
            seen.add(k)
    for k in to_write:
        if k not in seen:
            ordered.append(k)
            seen.add(k)

    body_lines = [f"{k}={_quote(merged[k])}" for k in ordered]
    body = "\n".join(body_lines) + "\n"

    # Atomic write in the parent dir so mode/ownership are correct on move.
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".env.", dir=str(profile_home), text=True
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, env_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # Some filesystems keep the previous file mode across os.replace; be
    # explicit about the final mode on env_path itself.
    os.chmod(env_path, mode)

    return env_path, list(to_write.keys())


def redact_key_names(keys: Iterable[str]) -> str:
    """Format a comma-separated list of KEY names — never values."""
    return ", ".join(sorted(set(keys))) or "(none)"
