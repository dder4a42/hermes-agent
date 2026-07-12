import json

import pytest


def test_initialize_research_copilot_home_copies_profile_files_and_scripts(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "profiles" / "alice"
    (source / "research-copilot").mkdir(parents=True)
    (source / "research-copilot" / "topics.json").write_text(json.dumps({"topics": [{"id": "agent"}]}))
    (source / "research-copilot" / "config.json").write_text(json.dumps({"pipeline": "test"}))
    (source / "scripts").mkdir()
    (source / "scripts" / "paper-fetch.py").write_text("print('fetch')\n")
    (source / "scripts" / "paper-health.py").write_text("print('health')\n")

    from research_copilot.bootstrap import initialize_research_copilot_home

    result = initialize_research_copilot_home(target, source_home=source)

    assert result["data_dir"] == str(target / "research-copilot")
    assert (target / "research-copilot" / "topics.json").exists()
    assert json.loads((target / "research-copilot" / "config.json").read_text())["pipeline"] == "test"
    assert (target / "research-copilot" / "candidates.jsonl").exists()
    assert (target / "research-copilot" / "recommendations.jsonl").exists()
    assert (target / "research-copilot" / "interactions.jsonl").exists()
    assert (target / "scripts" / "paper-fetch.py").read_text() == "print('fetch')\n"
    assert (target / "scripts" / "paper-health.py").read_text() == "print('health')\n"


def test_install_research_copilot_cron_is_profile_scoped_and_idempotent(tmp_path, monkeypatch):
    active_home = tmp_path / "default"
    profile_home = tmp_path / "profiles" / "alice"
    monkeypatch.setenv("HERMES_HOME", str(active_home))
    (profile_home / "scripts").mkdir(parents=True)
    (profile_home / "scripts" / "paper-fetch.py").write_text("print('fetch')\n")
    (profile_home / "scripts" / "paper-health.py").write_text("print('health')\n")

    from research_copilot.bootstrap import install_research_copilot_cron
    from cron.jobs import list_jobs, use_cron_store

    first = install_research_copilot_cron(profile_home, deliver="weixin")
    second = install_research_copilot_cron(profile_home, deliver="weixin")

    assert first["created"] == ["paper-fetcher", "daily-paper-pick", "paper-health-report"]
    assert second["created"] == []
    assert second["existing"] == ["paper-fetcher", "daily-paper-pick", "paper-health-report"]

    with use_cron_store(profile_home):
        jobs = list_jobs(include_disabled=True)
    assert [job["name"] for job in jobs] == ["paper-fetcher", "daily-paper-pick", "paper-health-report"]
    assert {job["deliver"] for job in jobs} == {"weixin"}

    with use_cron_store(active_home):
        assert list_jobs(include_disabled=True) == []


def test_repo_fallback_installs_scripts_and_skill_when_no_source(tmp_path):
    """With no source_home, bootstrap should still install repo-owned scripts
    and skill assets so a first-time profile is functional."""
    target = tmp_path / "profiles" / "bob"

    from research_copilot.bootstrap import initialize_research_copilot_home

    result = initialize_research_copilot_home(target)

    # Neutral JSON defaults created.
    assert (target / "research-copilot" / "config.json").exists()
    assert (target / "research-copilot" / "topics.json").exists()

    # Scripts land from repo (paper_fetch.py / paper_health.py copied to
    # dashed paper-fetch.py / paper-health.py names).
    fetch_dst = target / "scripts" / "paper-fetch.py"
    health_dst = target / "scripts" / "paper-health.py"
    assert fetch_dst.exists()
    assert health_dst.exists()
    # And they came from the repo, not from an empty file.
    assert fetch_dst.read_text().strip() != ""
    assert health_dst.read_text().strip() != ""

    # Skill assets present.
    skill_root = target / "skills" / "research" / "paper"
    assert (skill_root / "SKILL.md").exists()
    assert (skill_root / "references" / "format.md").exists()
    # Both are non-empty.
    assert (skill_root / "SKILL.md").read_text().strip() != ""

    # Reported keys present.
    assert result["profile_home"] == str(target.resolve())
    # scripts + skill assets show up in "copied" (first run, they didn't exist).
    joined = " ".join(result["copied"])
    assert "scripts/paper-fetch.py" in joined
    assert "SKILL.md" in joined


def test_default_force_false_preserves_existing_user_json(tmp_path):
    """Running bootstrap twice must not clobber a user's edited topics.json."""
    target = tmp_path / "profiles" / "carol"

    from research_copilot.bootstrap import initialize_research_copilot_home

    initialize_research_copilot_home(target)
    # User edits topics.json.
    topics_path = target / "research-copilot" / "topics.json"
    topics_path.write_text(json.dumps({"topics": [{"id": "custom", "name": "Custom"}]}))

    # Re-run bootstrap without --force; edits must survive.
    initialize_research_copilot_home(target)
    assert json.loads(topics_path.read_text())["topics"][0]["id"] == "custom"


def test_force_true_overwrites_user_json_from_defaults(tmp_path):
    """--force restores the neutral defaults (or the source_home template)."""
    target = tmp_path / "profiles" / "dan"

    from research_copilot.bootstrap import initialize_research_copilot_home

    initialize_research_copilot_home(target)
    topics_path = target / "research-copilot" / "topics.json"
    topics_path.write_text(json.dumps({"topics": [{"id": "custom"}]}))

    initialize_research_copilot_home(target, force=True)

    # Reset to neutral default (empty topics list).
    assert json.loads(topics_path.read_text()) == {"topics": []}


def test_force_never_deletes_jsonl_history(tmp_path):
    """Even with --force, candidates/recommendations/interactions must be preserved."""
    target = tmp_path / "profiles" / "eve"

    from research_copilot.bootstrap import initialize_research_copilot_home

    initialize_research_copilot_home(target)
    interactions = target / "research-copilot" / "interactions.jsonl"
    # Write some fake history.
    interactions.write_text(json.dumps({"kind": "save", "item_id": "p1", "at": "2026-07-01T00:00Z"}) + "\n")

    initialize_research_copilot_home(target, force=True)

    lines = [l for l in interactions.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["item_id"] == "p1"


def test_scripts_refresh_on_second_run_from_source(tmp_path):
    """When source_home is supplied, scripts should be refreshed even if the
    target already has them — they are code, not user state."""
    source = tmp_path / "source"
    target = tmp_path / "profiles" / "frank"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts" / "paper-fetch.py").write_text("v1\n")
    (source / "scripts" / "paper-health.py").write_text("v1-health\n")
    (source / "research-copilot").mkdir()

    from research_copilot.bootstrap import initialize_research_copilot_home

    initialize_research_copilot_home(target, source_home=source)
    assert (target / "scripts" / "paper-fetch.py").read_text() == "v1\n"

    # Source updates.
    (source / "scripts" / "paper-fetch.py").write_text("v2\n")
    initialize_research_copilot_home(target, source_home=source)
    assert (target / "scripts" / "paper-fetch.py").read_text() == "v2\n"


def test_bootstrap_never_touches_a_sibling_profile(tmp_path):
    """Bootstrapping profile bob must not create or modify anything under
    an unrelated alice profile."""
    alice = tmp_path / "profiles" / "alice"
    bob = tmp_path / "profiles" / "bob"

    from research_copilot.bootstrap import initialize_research_copilot_home

    initialize_research_copilot_home(alice)
    alice_state_before = {p: p.read_bytes() for p in alice.rglob("*") if p.is_file()}

    initialize_research_copilot_home(bob)

    alice_state_after = {p: p.read_bytes() for p in alice.rglob("*") if p.is_file()}
    assert alice_state_before.keys() == alice_state_after.keys()
    for path, content in alice_state_before.items():
        assert alice_state_after[path] == content
