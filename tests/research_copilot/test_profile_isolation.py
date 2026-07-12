"""Cross-profile isolation tests for Research Copilot.

Prove that bootstrapping profile A and then interacting with it does not
leak state into profile B's tree, and vice versa. This is the load-bearing
safety property for the one-profile-one-user product architecture.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_copilot.bootstrap import (
    initialize_research_copilot_home,
    install_research_copilot_cron,
)


@pytest.fixture
def two_profiles(tmp_path):
    alice = tmp_path / "profiles" / "alice"
    bob = tmp_path / "profiles" / "bob"
    initialize_research_copilot_home(alice)
    initialize_research_copilot_home(bob)
    return alice, bob


def _dir_snapshot(root: Path) -> dict[str, bytes]:
    """Full-content snapshot of every file under ``root``."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_appending_interaction_to_alice_leaves_bob_empty(two_profiles):
    alice, bob = two_profiles
    bob_before = _dir_snapshot(bob)

    ix_path = alice / "research-copilot" / "interactions.jsonl"
    ix_path.write_text(
        json.dumps({"kind": "save", "item_id": "p1", "at": "2026-07-12T09:00Z"}) + "\n"
    )

    bob_after = _dir_snapshot(bob)
    assert bob_after == bob_before
    # And Bob's interactions.jsonl is still empty.
    assert (bob / "research-copilot" / "interactions.jsonl").read_text() == ""


def test_topic_added_to_bob_leaves_alice_unchanged(two_profiles):
    alice, bob = two_profiles
    alice_before = _dir_snapshot(alice)

    bob_topics = bob / "research-copilot" / "topics.json"
    bob_topics.write_text(json.dumps({
        "topics": [{"id": "bob-only", "name": "Bob Only", "priority": 0.9, "status": "active"}]
    }))

    alice_after = _dir_snapshot(alice)
    assert alice_after == alice_before
    # Alice still shows the neutral default (empty topics list).
    assert json.loads((alice / "research-copilot" / "topics.json").read_text()) == {"topics": []}


def test_source_registry_edits_are_profile_local(two_profiles):
    alice, bob = two_profiles

    alice_reg = alice / "research-copilot" / "source_registry.json"
    alice_reg.write_text(json.dumps({"sources": [{"id": "alices-favorite-blog"}]}))

    bob_reg_body = (bob / "research-copilot" / "source_registry.json").read_text()
    assert "alices-favorite-blog" not in bob_reg_body


def test_candidates_written_to_alice_do_not_appear_in_bob(two_profiles):
    alice, bob = two_profiles
    (alice / "research-copilot" / "candidates.jsonl").write_text(
        json.dumps({"id": "alice-paper", "type": "paper", "title": "T",
                    "url": "u", "discovered_at": "2026-07-12T00:00Z",
                    "status": "candidate"}) + "\n"
    )
    bob_body = (bob / "research-copilot" / "candidates.jsonl").read_text()
    assert "alice-paper" not in bob_body
    assert bob_body == ""


def test_cron_bootstrap_is_scoped_per_profile(tmp_path, monkeypatch):
    """Installing cron jobs into Alice's profile must not create jobs in Bob's."""
    active = tmp_path / "active"  # unrelated active HERMES_HOME
    monkeypatch.setenv("HERMES_HOME", str(active))

    alice = tmp_path / "profiles" / "alice"
    bob = tmp_path / "profiles" / "bob"

    from cron.jobs import list_jobs, use_cron_store

    install_research_copilot_cron(alice, deliver="weixin")

    with use_cron_store(alice):
        alice_names = {j["name"] for j in list_jobs(include_disabled=True)}
    with use_cron_store(bob):
        bob_names = {j["name"] for j in list_jobs(include_disabled=True)}
    with use_cron_store(active):
        active_names = {j["name"] for j in list_jobs(include_disabled=True)}

    assert alice_names == {"paper-fetcher", "daily-paper-pick", "paper-health-report"}
    assert bob_names == set(), "installing cron in alice must not touch bob"
    assert active_names == set(), "installing cron in alice must not touch active profile"


def test_re_bootstrap_of_bob_preserves_alice_history(two_profiles):
    """Even a force=True re-bootstrap of Bob must never delete Alice's data."""
    alice, bob = two_profiles
    ix_path = alice / "research-copilot" / "interactions.jsonl"
    ix_path.write_text(
        json.dumps({"kind": "save", "item_id": "p1", "at": "2026-07-12T09:00Z"}) + "\n"
    )
    topics_path = alice / "research-copilot" / "topics.json"
    topics_path.write_text(json.dumps({"topics": [{"id": "alices-custom"}]}))

    initialize_research_copilot_home(bob, force=True)

    # Alice's edits survive.
    assert ix_path.read_text() != ""
    assert json.loads(topics_path.read_text())["topics"][0]["id"] == "alices-custom"


def test_alice_and_bob_have_independent_env_files(tmp_path):
    """Writing a secret to Alice's .env must not appear in Bob's tree."""
    from hermes_cli.env_writer import write_profile_env

    alice = tmp_path / "profiles" / "alice"
    bob = tmp_path / "profiles" / "bob"

    write_profile_env(alice, {"WEIXIN_TOKEN": "alice-only-secret"})

    # Bob's tree must not contain that string anywhere.
    for p in bob.rglob("*") if bob.exists() else []:
        if p.is_file():
            assert b"alice-only-secret" not in p.read_bytes()
