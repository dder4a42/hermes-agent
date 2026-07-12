import json
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


def _run(cmd: str, capsys) -> str:
    DummyCLI()._handle_paper_command(cmd)
    return capsys.readouterr().out


def test_topics_prints_active_topics(profile, capsys):
    _, data_dir = profile
    _write_json(data_dir / "topics.json", {
        "topics": [
            {"id": "research-agent", "name": "Research Agent",
             "priority": 0.98, "status": "active"},
            {"id": "old-topic", "name": "Old Topic",
             "priority": 0.4, "status": "dormant"},
        ],
    })
    out = _run("paper topics", capsys)
    assert "Research Copilot Topics" in out
    assert "research-agent" in out
    # Dormant topics still listed but sorted after active ones.
    assert out.index("research-agent") < out.index("old-topic")


def test_history_shows_recent_recommendations(profile, capsys):
    _, data_dir = profile
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "2607.08768", "recommended_at": "2026-07-12T09:00Z",
         "score": 0.86, "title": "UniClawBench",
         "topic_matches": ["Research Agent"]},
        {"id": "2607.01083", "recommended_at": "2026-07-12T14:00Z",
         "score": 0.9, "title": "Staleness-Learning RLHF",
         "topic_matches": ["RL Post-Training"]},
    ])
    out = _run("paper history 5", capsys)
    assert "Recent Research Picks" in out
    assert "2607.08768" in out
    assert "UniClawBench" in out
    assert "Staleness-Learning" in out


def test_save_records_interaction_and_updates_status(profile, capsys):
    _, data_dir = profile
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "2607.08768", "recommended_at": "2026-07-12T09:00Z",
         "score": 0.86, "title": "UniClawBench"},
    ])
    _write_jsonl(data_dir / "candidates.jsonl", [
        {"id": "2607.08768", "type": "paper", "title": "UniClawBench",
         "url": "https://arxiv.org/abs/2607.08768",
         "discovered_at": "2026-07-12T06:00Z", "status": "recommended"},
    ])

    out = _run("paper save 2607.08768", capsys)
    assert "Saved 2607.08768" in out

    ix_lines = (data_dir / "interactions.jsonl").read_text().strip().splitlines()
    assert len(ix_lines) == 1
    record = json.loads(ix_lines[0])
    assert record["item_id"] == "2607.08768"
    assert record.get("type") == "save" or record.get("kind") == "save"

    # Candidate status updated.
    for line in (data_dir / "candidates.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if obj.get("id") == "2607.08768":
            assert obj["status"] == "saved"


def test_skip_records_reason(profile, capsys):
    _, data_dir = profile
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "2607.99999", "recommended_at": "2026-07-12T09:00Z",
         "score": 0.7, "title": "Off-topic"},
    ])
    _write_jsonl(data_dir / "candidates.jsonl", [
        {"id": "2607.99999", "type": "paper", "title": "Off-topic",
         "url": "u", "discovered_at": "2026-07-12T06:00Z",
         "status": "recommended"},
    ])
    out = _run("paper skip 2607.99999 not relevant to current agenda", capsys)
    assert "Skipped 2607.99999" in out
    ix = json.loads((data_dir / "interactions.jsonl").read_text().strip())
    assert ix["item_id"] == "2607.99999"
    assert ix.get("reason") == "not relevant to current agenda"


def test_read_marks_status(profile, capsys):
    _, data_dir = profile
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "p1", "recommended_at": "2026-07-12T09:00Z",
         "score": 0.9, "title": "P1"},
    ])
    _write_jsonl(data_dir / "candidates.jsonl", [
        {"id": "p1", "type": "paper", "title": "P1", "url": "u",
         "discovered_at": "2026-07-12T06:00Z", "status": "saved"},
    ])
    out = _run("paper read p1", capsys)
    assert "Marked read p1" in out


def test_feedback_requires_text(profile, capsys):
    _, data_dir = profile
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "p1", "recommended_at": "2026-07-12T09:00Z",
         "score": 0.9, "title": "P1"},
    ])
    out = _run("paper feedback p1", capsys)
    assert "Usage: /paper feedback" in out
    # No interaction should have been recorded.
    assert not (data_dir / "interactions.jsonl").exists() or \
        (data_dir / "interactions.jsonl").read_text().strip() == ""


def test_feedback_records_text(profile, capsys):
    _, data_dir = profile
    _write_jsonl(data_dir / "recommendations.jsonl", [
        {"id": "p1", "recommended_at": "2026-07-12T09:00Z",
         "score": 0.9, "title": "P1"},
    ])
    out = _run("paper feedback p1 this is exactly what I want more of", capsys)
    assert "Recorded feedback" in out
    ix = json.loads((data_dir / "interactions.jsonl").read_text().strip())
    assert ix["item_id"] == "p1"
    assert ix.get("text") == "this is exactly what I want more of"


def test_unknown_id_returns_helpful_message(profile, capsys):
    _, data_dir = profile
    _write_jsonl(data_dir / "recommendations.jsonl", [])
    out = _run("paper save does-not-exist", capsys)
    assert "not found" in out.lower()
    assert "/paper history" in out


def test_health_returns_report(profile, capsys):
    _, data_dir = profile
    _write_json(data_dir / "state.json", {})
    _write_json(data_dir / "topics.json", {"topics": []})
    _write_jsonl(data_dir / "recommendations.jsonl", [])
    _write_jsonl(data_dir / "interactions.jsonl", [])
    _write_jsonl(data_dir / "candidates.jsonl", [])
    out = _run("paper health", capsys)
    assert "Research Copilot" in out
    assert "Recommendations delivered:" in out


def test_bare_paper_prints_usage(profile, capsys):
    _, data_dir = profile
    _write_json(data_dir / "topics.json", {"topics": []})
    out = _run("paper", capsys)
    assert "Research Copilot" in out
    assert "/paper topics" in out
