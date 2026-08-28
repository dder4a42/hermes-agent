import json

import pytest


def test_initialize_research_copilot_home_copies_profile_files_without_legacy_scripts(tmp_path):
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
    assert not (target / "scripts" / "paper-fetch.py").exists()
    assert not (target / "scripts" / "paper-health.py").exists()


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

    expected = ["paper-fetcher", "research-library-recommend", "paper-health-report", "task-surfacer", "thought-surfacer"]
    assert first["created"] == expected
    assert second["created"] == []
    assert second["existing"] == expected

    with use_cron_store(profile_home):
        jobs = list_jobs(include_disabled=True)
    assert [job["name"] for job in jobs] == expected
    assert {job["deliver"] for job in jobs} == {"weixin"}
    scripts = {job["name"]: job.get("script") for job in jobs}
    assert scripts["paper-fetcher"] == "module:research_copilot.scripts.library_collect"
    assert scripts["paper-health-report"] == "module:research_copilot.scripts.library_health"
    recommend = next(job for job in jobs if job["name"] == "research-library-recommend")
    assert recommend["script"] == "module:research_copilot.scripts.library_recommend"
    assert recommend["no_agent"] is False
    assert "主体叙述" in recommend["prompt"]
    assert "current_beliefs" in recommend["prompt"]
    assert not (profile_home / "scripts" / "paper-fetch.py").exists()
    assert not (profile_home / "scripts" / "paper-health.py").exists()

    with use_cron_store(active_home):
        assert list_jobs(include_disabled=True) == []


def test_repo_fallback_installs_skill_without_legacy_paper_scripts(tmp_path):
    """Bundled modules replace copied Research Copilot implementation files."""
    target = tmp_path / "profiles" / "bob"

    from research_copilot.bootstrap import initialize_research_copilot_home

    result = initialize_research_copilot_home(target)

    # Neutral JSON defaults created.
    assert (target / "research-copilot" / "config.json").exists()
    assert (target / "research-copilot" / "topics.json").exists()

    # Research business logic stays in the installed package.
    fetch_dst = target / "scripts" / "paper-fetch.py"
    health_dst = target / "scripts" / "paper-health.py"
    assert not fetch_dst.exists()
    assert not health_dst.exists()
    assert (target / "scripts" / "task-surfacer.py").exists()
    assert (target / "scripts" / "thought-surfacer.py").exists()

    # Skill assets present.
    skill_root = target / "skills" / "research" / "paper"
    assert (skill_root / "SKILL.md").exists()
    assert (skill_root / "references" / "format.md").exists()
    # Both are non-empty.
    assert (skill_root / "SKILL.md").read_text().strip() != ""

    # Reported keys present.
    assert result["profile_home"] == str(target.resolve())
    # Only still-managed assets show up in "copied".
    joined = " ".join(result["copied"])
    assert "scripts/paper-fetch.py" not in joined
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


def test_legacy_paper_scripts_are_not_restored_from_source(tmp_path):
    """A template profile cannot reintroduce removed bundled business code."""
    source = tmp_path / "source"
    target = tmp_path / "profiles" / "frank"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts" / "paper-fetch.py").write_text("v1\n")
    (source / "scripts" / "paper-health.py").write_text("v1-health\n")
    (source / "research-copilot").mkdir()

    from research_copilot.bootstrap import initialize_research_copilot_home

    initialize_research_copilot_home(target, source_home=source)
    assert not (target / "scripts" / "paper-fetch.py").exists()
    assert not (target / "scripts" / "paper-health.py").exists()

    # A later source update must not copy the retired implementation back.
    (source / "scripts" / "paper-fetch.py").write_text("v2\n")
    initialize_research_copilot_home(target, source_home=source)
    assert not (target / "scripts" / "paper-fetch.py").exists()


def test_bootstrap_accepts_managed_script_symlinked_to_repo(tmp_path):
    target = tmp_path / "profiles" / "linked"
    scripts = target / "scripts"
    scripts.mkdir(parents=True)

    from research_copilot import bootstrap

    source = bootstrap._REPO_ROOT / "scripts" / "task_surfacer.py"
    (scripts / "task-surfacer.py").symlink_to(source)

    result = bootstrap.initialize_research_copilot_home(target)

    assert (scripts / "task-surfacer.py").samefile(source)
    assert "scripts/task-surfacer.py" not in result["refreshed"]


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
