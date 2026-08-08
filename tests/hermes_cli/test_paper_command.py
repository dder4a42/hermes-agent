import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli.cli_commands_mixin import CLICommandsMixin


class DummyCLI(CLICommandsMixin):
    pass


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir()
    return home, data_dir


def _write_json(path: Path, obj):
    path.write_text(json.dumps(obj))


def _write_jsonl(path: Path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""))


def _seed_recommendation(data_dir: Path, *, title: str, recommended_at: datetime) -> str:
    from research_copilot.library import ResearchItemDraft, SourceEvidence
    from research_copilot.runtime import open_library

    connection, repository = open_library(data_dir / "library.db")
    repository.upsert_source(
        source_id="test", provider="test", display_name="Test", source_type="paper",
        tier=0.9, now=recommended_at,
    )
    item = repository.upsert_item(
        ResearchItemDraft(title=title, url=f"https://example.com/{title}"),
        source=SourceEvidence("test"), discovered_at=recommended_at,
    )
    repository.record_recommendation(
        item.item_id, score=0.9, score_breakdown={}, recommended_at=recommended_at,
    )
    connection.close()
    return item.item_id


def _run(cmd: str, capsys) -> str:
    DummyCLI()._handle_paper_command(cmd)
    return capsys.readouterr().out


def test_topics_prints_active_topics(profile, capsys):
    _, data_dir = profile
    (data_dir / "topics.yaml").write_text("""
topics:
  - {id: research-agent, name: Research Agent, priority: 0.98, status: active}
  - {id: old-topic, name: Old Topic, priority: 0.4, status: dormant}
""")
    (data_dir / "research-profile.yaml").write_text("""
long_term_agenda:
  - {id: research-agent, priority: 0.98}
  - {id: old-topic, priority: 0.4}
""")
    out = _run("paper topics", capsys)
    assert "Research Copilot Topics" in out
    assert "research-agent" in out
    # Dormant topics still listed but sorted after active ones.
    assert out.index("research-agent") < out.index("old-topic")


def test_history_shows_recent_recommendations(profile, capsys):
    _, data_dir = profile
    old = _seed_recommendation(
        data_dir, title="UniClawBench",
        recommended_at=datetime(2026, 7, 12, 9, tzinfo=timezone.utc),
    )
    new = _seed_recommendation(
        data_dir, title="Staleness-Learning RLHF",
        recommended_at=datetime(2026, 7, 12, 14, tzinfo=timezone.utc),
    )
    out = _run("paper history 5", capsys)
    assert "Recent Research Picks" in out
    assert old in out
    assert new in out
    assert "UniClawBench" in out
    assert "Staleness-Learning" in out


def test_save_records_interaction_and_updates_status(profile, capsys):
    _, data_dir = profile
    item_id = _seed_recommendation(
        data_dir, title="UniClawBench",
        recommended_at=datetime(2026, 7, 12, 9, tzinfo=timezone.utc),
    )

    out = _run(f"paper save {item_id}", capsys)
    assert f"Saved {item_id}" in out
    from research_copilot.runtime import open_library
    connection, _ = open_library(data_dir / "library.db")
    row = connection.execute(
        "SELECT workflow_state,user_learning_status FROM research_items WHERE id=?", (item_id,),
    ).fetchone()
    assert (row["workflow_state"], row["user_learning_status"]) == ("discovered", "saved")
    assert connection.execute(
        "SELECT kind FROM feedback_events WHERE item_id=?", (item_id,),
    ).fetchone()["kind"] == "save"
    connection.close()


def test_skip_records_reason(profile, capsys):
    _, data_dir = profile
    item_id = _seed_recommendation(
        data_dir, title="Off-topic",
        recommended_at=datetime(2026, 7, 12, 9, tzinfo=timezone.utc),
    )
    out = _run(f"paper skip {item_id} not relevant to current agenda", capsys)
    assert f"Skipped {item_id}" in out
    from research_copilot.runtime import open_library
    connection, _ = open_library(data_dir / "library.db")
    row = connection.execute(
        "SELECT payload_json FROM feedback_events WHERE item_id=?", (item_id,),
    ).fetchone()
    assert json.loads(row["payload_json"])["reason"] == "not relevant to current agenda"
    connection.close()


def test_read_marks_status(profile, capsys):
    _, data_dir = profile
    item_id = _seed_recommendation(
        data_dir, title="P1",
        recommended_at=datetime(2026, 7, 12, 9, tzinfo=timezone.utc),
    )
    out = _run(f"paper read {item_id}", capsys)
    assert f"Marked read {item_id}" in out


def test_feedback_requires_text(profile, capsys):
    _, data_dir = profile
    out = _run("paper feedback p1", capsys)
    assert "Usage: /paper feedback" in out
    # No interaction should have been recorded.
    assert not (data_dir / "library.db").exists()


def test_feedback_records_text(profile, capsys):
    _, data_dir = profile
    item_id = _seed_recommendation(
        data_dir, title="P1",
        recommended_at=datetime(2026, 7, 12, 9, tzinfo=timezone.utc),
    )
    out = _run(f"paper feedback {item_id} this is exactly what I want more of", capsys)
    assert "Recorded feedback" in out
    from research_copilot.runtime import open_library
    connection, _ = open_library(data_dir / "library.db")
    row = connection.execute(
        "SELECT payload_json FROM feedback_events WHERE item_id=?", (item_id,),
    ).fetchone()
    assert json.loads(row["payload_json"])["text"] == "this is exactly what I want more of"
    connection.close()


def test_unknown_id_returns_helpful_message(profile, capsys):
    _, data_dir = profile
    out = _run("paper save does-not-exist", capsys)
    assert "not found" in out.lower()
    assert "/paper history" in out


def test_health_returns_report(profile, capsys):
    _, data_dir = profile
    out = _run("paper health", capsys)
    assert "Research Library health" in out


def test_bare_paper_prints_usage(profile, capsys):
    _, data_dir = profile
    out = _run("paper", capsys)
    assert "Research Copilot" in out
    assert "/paper topics" in out
