import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway.slash_commands import GatewaySlashCommandsMixin


class DummyGateway(GatewaySlashCommandsMixin):
    pass


class DummyEvent:
    def __init__(self, args: str):
        self._args = args

    def get_command_args(self) -> str:
        return self._args


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    data_dir = home / "research-copilot"
    data_dir.mkdir()
    return home, data_dir


def _run(args: str) -> str:
    return asyncio.run(DummyGateway()._handle_paper_command(DummyEvent(args)))


def _write_json(path: Path, obj):
    path.write_text(json.dumps(obj))


def _write_jsonl(path: Path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""))


def _seed_recommendation(data_dir: Path, *, item_id_title="P1") -> str:
    from research_copilot.library import ResearchItemDraft, SourceEvidence
    from research_copilot.runtime import open_library

    now = datetime(2026, 7, 12, 9, tzinfo=timezone.utc)
    connection, repository = open_library(data_dir / "library.db")
    repository.upsert_source(
        source_id="test", provider="test", display_name="Test", source_type="paper",
        tier=.9, now=now,
    )
    item = repository.upsert_item(
        ResearchItemDraft(title=item_id_title, url=f"https://example.com/{item_id_title}"),
        source=SourceEvidence("test"), discovered_at=now,
    )
    repository.record_recommendation(
        item.item_id, score=.9, score_breakdown={"topic_relevance": .9}, recommended_at=now,
    )
    connection.close()
    return item.item_id


def test_topics_lists_active(profile):
    _, data_dir = profile
    (data_dir / "topics.yaml").write_text(
        "topics:\n  - id: research-agent\n    name: Research Agent\n    priority: 0.98\n    status: active\n"
    )
    out = _run("topics")
    assert "Research Copilot Topics" in out
    assert "research-agent" in out


def test_history_lists_recent(profile):
    _, data_dir = profile
    item_id = _seed_recommendation(data_dir, item_id_title="UniClawBench")
    out = _run("history 3")
    assert item_id in out
    assert "UniClawBench" in out


def test_save_appends_to_interactions_jsonl(profile):
    _, data_dir = profile
    item_id = _seed_recommendation(data_dir)
    out = _run(f"save {item_id}")
    assert f"Saved {item_id}" in out
    from research_copilot.runtime import open_library
    connection, _ = open_library(data_dir / "library.db")
    assert connection.execute("SELECT kind FROM feedback_events WHERE item_id=?", (item_id,)).fetchone()[0] == "save"
    connection.close()


def test_skip_captures_reason(profile):
    _, data_dir = profile
    item_id = _seed_recommendation(data_dir)
    out = _run(f"skip {item_id} too tangential right now")
    assert f"Skipped {item_id}" in out
    from research_copilot.runtime import open_library
    connection, _ = open_library(data_dir / "library.db")
    payload = connection.execute("SELECT payload_json FROM feedback_events WHERE item_id=?", (item_id,)).fetchone()[0]
    assert json.loads(payload)["reason"] == "too tangential right now"
    connection.close()


def test_feedback_appends_text(profile):
    _, data_dir = profile
    item_id = _seed_recommendation(data_dir)
    out = _run(f"feedback {item_id} give me more like this")
    assert "Recorded feedback" in out
    from research_copilot.runtime import open_library
    connection, _ = open_library(data_dir / "library.db")
    payload = connection.execute("SELECT payload_json FROM feedback_events WHERE item_id=?", (item_id,)).fetchone()[0]
    assert json.loads(payload)["text"] == "give me more like this"
    connection.close()


def test_unknown_id_says_not_found(profile):
    _, data_dir = profile
    out = _run("save nope")
    assert "not found" in out.lower()


def test_health_returns_report(profile):
    _, data_dir = profile
    out = _run("health")
    assert "Research Library health" in out
