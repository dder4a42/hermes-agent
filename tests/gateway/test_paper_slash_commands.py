import asyncio
import json
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


def test_topics_lists_active(profile):
    _, data_dir = profile
    _write_json(data_dir / "topics.json", {
        "topics": [
            {"id": "research-agent", "name": "Research Agent",
             "priority": 0.98, "status": "active"},
        ],
    })
    out = _run("topics")
    assert "Research Copilot Topics" in out
    assert "research-agent" in out


def test_history_lists_recent(profile):
    _, data_dir = profile
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "2607.08768", "recommended_at": "2026-07-12T09:00Z",
         "score": 0.86, "title": "UniClawBench",
         "topic_matches": ["Research Agent"]},
    ])
    out = _run("history 3")
    assert "2607.08768" in out
    assert "UniClawBench" in out


def test_save_appends_to_interactions_jsonl(profile):
    _, data_dir = profile
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "p1", "recommended_at": "2026-07-12T09:00Z",
         "score": 0.9, "title": "P1"},
    ])
    _write_jsonl(data_dir / "candidates.jsonl", [
        {"id": "p1", "type": "paper", "title": "P1", "url": "u",
         "discovered_at": "2026-07-12T06:00Z", "status": "recommended"},
    ])
    out = _run("save p1")
    assert "Saved p1" in out
    ix = json.loads((data_dir / "interactions.jsonl").read_text().strip())
    assert ix["item_id"] == "p1"


def test_skip_captures_reason(profile):
    _, data_dir = profile
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "p1", "recommended_at": "2026-07-12T09:00Z",
         "score": 0.7, "title": "P1"},
    ])
    _write_jsonl(data_dir / "candidates.jsonl", [
        {"id": "p1", "type": "paper", "title": "P1", "url": "u",
         "discovered_at": "2026-07-12T06:00Z", "status": "recommended"},
    ])
    out = _run("skip p1 too tangential right now")
    assert "Skipped p1" in out
    ix = json.loads((data_dir / "interactions.jsonl").read_text().strip())
    assert ix.get("reason") == "too tangential right now"


def test_feedback_appends_text(profile):
    _, data_dir = profile
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "p1", "recommended_at": "2026-07-12T09:00Z",
         "score": 0.9, "title": "P1"},
    ])
    out = _run("feedback p1 give me more like this")
    assert "Recorded feedback" in out
    ix = json.loads((data_dir / "interactions.jsonl").read_text().strip())
    assert ix.get("text") == "give me more like this"


def test_unknown_id_says_not_found(profile):
    _, data_dir = profile
    _write_jsonl(data_dir / "recommendations.jsonl", [])
    out = _run("save nope")
    assert "not found" in out.lower()


def test_health_returns_report(profile):
    _, data_dir = profile
    _write_json(data_dir / "state.json", {})
    _write_json(data_dir / "topics.json", {"topics": []})
    _write_jsonl(data_dir / "recommendations.jsonl", [])
    _write_jsonl(data_dir / "interactions.jsonl", [])
    _write_jsonl(data_dir / "candidates.jsonl", [])
    out = _run("health")
    assert "Research Copilot" in out
    assert "Recommendations delivered:" in out
