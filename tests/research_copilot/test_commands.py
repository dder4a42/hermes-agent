import json
from datetime import datetime, timezone


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _seed_recommendation(home, *, title="Paper One", recommended_at=None):
    from research_copilot.library import ResearchItemDraft, SourceEvidence
    from research_copilot.runtime import open_library

    connection, repository = open_library(home / "research-copilot" / "library.db")
    now = recommended_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
    repository.upsert_source(
        source_id="test", provider="test", display_name="Test", source_type="paper",
        tier=.9, now=now,
    )
    item = repository.upsert_item(
        ResearchItemDraft(title=title, url=f"https://example.com/{title}"),
        source=SourceEvidence("test"), discovered_at=now,
    )
    repository.record_recommendation(
        item.item_id, score=.8, score_breakdown={"topic_relevance": .8},
        recommended_at=now,
    )
    connection.close()
    return item.item_id


def test_paper_topics_lists_active_and_dormant_topics(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir(parents=True)
    (data_dir / "topics.yaml").write_text("""
topics:
  - id: research-agent
    name: Research Agent
    priority: 0.98
    status: active
  - id: world-model
    name: World Model
    priority: 0.35
    status: dormant
""")

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
    old = _seed_recommendation(home, title="Old Paper", recommended_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    new = _seed_recommendation(home, title="New Paper", recommended_at=datetime(2026, 1, 2, tzinfo=timezone.utc))

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command("history 1")

    assert "Recent Research Picks" in output
    assert new in output
    assert "New Paper" in output
    assert old not in output


def test_paper_save_records_interaction_and_updates_candidate(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    item_id = _seed_recommendation(home)

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command(f"save {item_id}")

    assert f"Saved {item_id}" in output
    from research_copilot.runtime import open_library
    connection, _ = open_library(home / "research-copilot" / "library.db")
    assert connection.execute("SELECT status FROM research_items WHERE id=?", (item_id,)).fetchone()[0] == "saved"
    assert connection.execute("SELECT kind FROM feedback_events WHERE item_id=?", (item_id,)).fetchone()[0] == "save"
    connection.close()


def test_paper_skip_records_reason(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    item_id = _seed_recommendation(home)

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command(f"skip {item_id} too benchmark-heavy")

    assert f"Skipped {item_id}" in output
    from research_copilot.runtime import open_library
    connection, _ = open_library(home / "research-copilot" / "library.db")
    row = connection.execute("SELECT kind,payload_json FROM feedback_events WHERE item_id=?", (item_id,)).fetchone()
    assert row["kind"] == "skip"
    assert json.loads(row["payload_json"])["reason"] == "too benchmark-heavy"
    connection.close()


def test_paper_feedback_records_free_text(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))
    item_id = _seed_recommendation(home)

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command(f"feedback {item_id} useful for harness design")

    assert f"Recorded feedback for {item_id}" in output
    from research_copilot.runtime import open_library
    connection, _ = open_library(home / "research-copilot" / "library.db")
    row = connection.execute("SELECT kind,payload_json FROM feedback_events WHERE item_id=?", (item_id,)).fetchone()
    assert row["kind"] == "note"
    assert json.loads(row["payload_json"])["text"] == "useful for harness design"
    connection.close()



def test_paper_now_triggers_library_recommend_cron(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))

    # cron.jobs captures HERMES_DIR at module import time (see the frozen
    # module-level CRON_DIR/JOBS_FILE constants). Whichever test imports it
    # first pins the path for the whole session, so tests relying only on
    # monkeypatch.setenv silently write to the previous test's tmpdir or the
    # real ~/.hermes. Mirror the production code path in _trigger_daily_pick
    # and wrap create_job in use_cron_store(home) so writes land where reads
    # will look.
    from cron.jobs import create_job, get_job, use_cron_store

    with use_cron_store(home):
        job = create_job(
            prompt=None,
            schedule="0 8 * * *",
            name="research-library-recommend",
            deliver="weixin",
            script="module:research_copilot.scripts.library_recommend",
            no_agent=True,
        )

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command("now")

    assert "Triggered research-library-recommend" in output
    with use_cron_store(home):
        updated = get_job(job["id"])
    assert updated is not None
    assert updated["next_run_at"] is not None


def test_paper_now_reports_missing_recommendation_job(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))

    from research_copilot.commands import handle_paper_command

    output = handle_paper_command("now")

    assert "recommendation cron job was not found" in output
    assert "hermes cron" in output
