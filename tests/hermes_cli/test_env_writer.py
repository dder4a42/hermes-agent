"""Tests for hermes_cli.env_writer."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hermes_cli.env_writer import EnvKeyConflict, write_profile_env


def test_creates_env_file_at_mode_0600(tmp_path):
    home = tmp_path / "alice"
    home.mkdir()

    path, keys = write_profile_env(home, {"WEIXIN_TOKEN": "secret123"})

    assert path == home / ".env"
    assert path.exists()
    file_mode = stat.S_IMODE(path.stat().st_mode)
    assert file_mode == 0o600
    assert keys == ["WEIXIN_TOKEN"]


def test_written_value_appears_in_file(tmp_path):
    home = tmp_path / "alice"
    home.mkdir()

    write_profile_env(home, {"WEIXIN_ACCOUNT_ID": "acct-42"})

    body = (home / ".env").read_text()
    assert "WEIXIN_ACCOUNT_ID=acct-42" in body


def test_empty_values_are_skipped(tmp_path):
    home = tmp_path / "alice"
    home.mkdir()

    path, keys = write_profile_env(home, {
        "WEIXIN_TOKEN": "real",
        "WEIXIN_ACCOUNT_ID": "",
        "WEIXIN_DM_POLICY": None,
    })

    assert keys == ["WEIXIN_TOKEN"]
    body = path.read_text()
    assert "WEIXIN_TOKEN=real" in body
    assert "WEIXIN_ACCOUNT_ID" not in body
    assert "WEIXIN_DM_POLICY" not in body


def test_refuses_to_overwrite_existing_key(tmp_path):
    home = tmp_path / "alice"
    home.mkdir()
    (home / ".env").write_text("WEIXIN_TOKEN=old\n")

    with pytest.raises(EnvKeyConflict) as excinfo:
        write_profile_env(home, {"WEIXIN_TOKEN": "new"})
    assert "WEIXIN_TOKEN" in excinfo.value.conflicting

    # File must be unchanged.
    assert (home / ".env").read_text() == "WEIXIN_TOKEN=old\n"


def test_overwrite_true_replaces_value(tmp_path):
    home = tmp_path / "alice"
    home.mkdir()
    (home / ".env").write_text("WEIXIN_TOKEN=old\n")

    write_profile_env(home, {"WEIXIN_TOKEN": "new"}, overwrite=True)

    body = (home / ".env").read_text()
    assert "WEIXIN_TOKEN=new" in body
    assert "old" not in body


def test_merges_new_keys_without_touching_existing(tmp_path):
    home = tmp_path / "alice"
    home.mkdir()
    (home / ".env").write_text("EXISTING_KEY=keep\nOTHER=also\n")

    write_profile_env(home, {"NEW_KEY": "added"})

    body = (home / ".env").read_text()
    assert "EXISTING_KEY=keep" in body
    assert "OTHER=also" in body
    assert "NEW_KEY=added" in body


def test_values_with_spaces_get_quoted(tmp_path):
    home = tmp_path / "alice"
    home.mkdir()

    write_profile_env(home, {"NOTE": "hello world"})

    body = (home / ".env").read_text()
    assert 'NOTE="hello world"' in body


def test_comma_separated_allowed_users_not_quoted(tmp_path):
    home = tmp_path / "alice"
    home.mkdir()

    write_profile_env(home, {"WEIXIN_ALLOWED_USERS": "u1,u2,u3"})

    body = (home / ".env").read_text()
    assert "WEIXIN_ALLOWED_USERS=u1,u2,u3" in body


def test_creates_parent_directories(tmp_path):
    home = tmp_path / "not-yet-created" / "alice"

    path, _ = write_profile_env(home, {"WEIXIN_TOKEN": "x"})

    assert path.parent.exists()
    assert path.exists()


def test_atomic_replace_no_dot_env_leftover(tmp_path):
    home = tmp_path / "alice"
    home.mkdir()

    write_profile_env(home, {"WEIXIN_TOKEN": "x"})

    leftovers = [p for p in home.iterdir() if p.name.startswith(".env.")]
    assert leftovers == [], f"tempfile leaked: {leftovers}"
