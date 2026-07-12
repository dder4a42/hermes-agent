import json


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
