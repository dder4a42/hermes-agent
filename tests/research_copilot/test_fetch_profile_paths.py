"""Profile-safety tests for research_copilot/scripts/paper_fetch.py.

These tests prove that setting HERMES_HOME=/tmp/whatever confines every
path the fetcher module uses to that directory. No real fetching happens
here — that would hit the network and be flaky in CI. We only verify that
module-level path resolution reads HERMES_HOME correctly and that nothing
falls back to Path.home() / ".hermes".
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def isolated_profile(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir and force a fresh module import."""
    profile_home = tmp_path / "profile-a"
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    # Also clear any hint that could leak the real user's home.
    monkeypatch.delenv("RESEARCH_COPILOT_ARXIV_FALLBACK", raising=False)
    monkeypatch.delenv("RESEARCH_COPILOT_GMAIL_ADDR", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    # Force re-import so module-level path constants pick up the new
    # HERMES_HOME instead of the value captured in the previous test.
    sys.modules.pop("research_copilot.scripts.paper_fetch", None)
    module = importlib.import_module("research_copilot.scripts.paper_fetch")
    yield profile_home, module
    sys.modules.pop("research_copilot.scripts.paper_fetch", None)


def test_hermes_home_reads_env(isolated_profile):
    profile_home, module = isolated_profile
    assert module.HERMES_HOME == profile_home


def test_data_dir_lives_under_profile(isolated_profile):
    profile_home, module = isolated_profile
    assert module.DATA_DIR == profile_home / "research-copilot"


@pytest.mark.parametrize(
    "attr,leaf",
    [
        ("TOPICS_PATH", "topics.json"),
        ("SOURCE_REGISTRY_PATH", "source_registry.json"),
        ("RESEARCH_PROFILE_PATH", "research_profile.json"),
        ("CANDIDATES_PATH", "candidates.jsonl"),
        ("STATE_PATH", "state.json"),
    ],
)
def test_every_file_path_confined_to_profile(isolated_profile, attr, leaf):
    profile_home, module = isolated_profile
    path: Path = getattr(module, attr)
    # Path must be inside the profile home. is_relative_to() would work on
    # 3.9+ but the codebase supports older Pythons in some cron contexts;
    # fall back to a startswith check.
    profile_str = str(profile_home)
    assert str(path).startswith(profile_str + "/"), (
        f"{attr} = {path} escapes profile home {profile_home}"
    )
    assert path.name == leaf


def test_switching_hermes_home_reroutes_paths(tmp_path, monkeypatch):
    """A second profile with a fresh HERMES_HOME must see a different DATA_DIR."""
    alice = tmp_path / "alice"
    alice.mkdir()
    bob = tmp_path / "bob"
    bob.mkdir()

    monkeypatch.setenv("HERMES_HOME", str(alice))
    sys.modules.pop("research_copilot.scripts.paper_fetch", None)
    mod_alice = importlib.import_module("research_copilot.scripts.paper_fetch")
    alice_dir = mod_alice.DATA_DIR

    monkeypatch.setenv("HERMES_HOME", str(bob))
    sys.modules.pop("research_copilot.scripts.paper_fetch", None)
    mod_bob = importlib.import_module("research_copilot.scripts.paper_fetch")
    bob_dir = mod_bob.DATA_DIR

    assert alice_dir == alice / "research-copilot"
    assert bob_dir == bob / "research-copilot"
    assert alice_dir != bob_dir


def test_arxiv_fallback_off_by_default(isolated_profile):
    _, module = isolated_profile
    assert module.ENABLE_ARXIV_FALLBACK is False


def test_arxiv_fallback_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("RESEARCH_COPILOT_ARXIV_FALLBACK", "1")
    sys.modules.pop("research_copilot.scripts.paper_fetch", None)
    module = importlib.import_module("research_copilot.scripts.paper_fetch")
    assert module.ENABLE_ARXIV_FALLBACK is True


def test_no_personal_email_baked_in(isolated_profile):
    """The port must not have kept the original operator's Gmail hardcoded."""
    _, module = isolated_profile
    # GMAIL_ADDR comes purely from env; with the env clean it should be empty.
    assert module.GMAIL_ADDR == ""


def test_gmail_addr_reads_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("RESEARCH_COPILOT_GMAIL_ADDR", "alice@example.com")
    sys.modules.pop("research_copilot.scripts.paper_fetch", None)
    module = importlib.import_module("research_copilot.scripts.paper_fetch")
    assert module.GMAIL_ADDR == "alice@example.com"
