import json


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_paper_topics_lists_active_and_dormant_topics(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir(parents=True)
    (data_dir / "topics.json").write_text(json.dumps({
        "topics": [
            {"id": "research-agent", "name": "Research Agent", "priority": 0.98, "status": "active"},
            {"id": "world-model", "name": "World Model", "priority": 0.35, "status": "dormant"},
        ]
    }))

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command("topics")

    assert "Research Copilot Topics" in output
    assert "research-agent" in output
    assert "Research Agent" in output
    assert "active" in output
    assert "world-model" in output
    assert "dormant" in output


def test_paper_history_lists_recent_recommendations(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir(parents=True)
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "old", "title": "Old Paper", "recommended_at": "2026-01-01T00:00:00Z", "topic_matches": ["Agent Infra"]},
        {"id": "new", "title": "New Paper", "recommended_at": "2026-01-02T00:00:00Z", "topic_matches": ["Research Agent"]},
    ])

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command("history 1")

    assert "Recent Research Picks" in output
    assert "new" in output
    assert "New Paper" in output
    assert "old" not in output


def test_paper_save_records_interaction_and_updates_candidate(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir(parents=True)
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "paper-1", "title": "Paper One", "recommended_at": "2026-01-01T00:00:00Z"},
    ])
    _write_jsonl(data_dir / "candidates.jsonl", [
        {"id": "paper-1", "title": "Paper One", "status": "candidate"},
    ])

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command("save paper-1")

    assert "Saved paper-1" in output
    interactions = [json.loads(line) for line in (data_dir / "interactions.jsonl").read_text().splitlines()]
    assert interactions[0]["type"] == "save"
    assert interactions[0]["item_id"] == "paper-1"
    candidates = [json.loads(line) for line in (data_dir / "candidates.jsonl").read_text().splitlines()]
    assert candidates[0]["status"] == "saved"


def test_paper_skip_records_reason(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir(parents=True)
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "paper-1", "title": "Paper One", "recommended_at": "2026-01-01T00:00:00Z"},
    ])

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command("skip paper-1 too benchmark-heavy")

    assert "Skipped paper-1" in output
    interactions = [json.loads(line) for line in (data_dir / "interactions.jsonl").read_text().splitlines()]
    assert interactions[0]["type"] == "skip"
    assert interactions[0]["item_id"] == "paper-1"
    assert interactions[0]["reason"] == "too benchmark-heavy"


def test_paper_feedback_records_free_text(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir(parents=True)
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "paper-1", "title": "Paper One", "recommended_at": "2026-01-01T00:00:00Z"},
    ])

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command("feedback paper-1 useful for harness design")

    assert "Recorded feedback for paper-1" in output
    interactions = [json.loads(line) for line in (data_dir / "interactions.jsonl").read_text().splitlines()]
    assert interactions[0]["type"] == "feedback"
    assert interactions[0]["item_id"] == "paper-1"
    assert interactions[0]["text"] == "useful for harness design"



def test_paper_now_triggers_daily_paper_pick_cron(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))

    from cron.jobs import create_job, get_job

    job = create_job(
        prompt="pick a paper",
        schedule="0 8 * * *",
        name="daily-paper-pick",
        deliver="weixin",
    )

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command("now")

    assert "Triggered daily-paper-pick" in output
    updated = get_job(job["id"])
    assert updated is not None
    assert updated["next_run_at"] is not None


def test_paper_now_reports_missing_daily_pick_job(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command("now")

    assert "daily-paper-pick cron job was not found" in output
    assert "hermes cron" in output
