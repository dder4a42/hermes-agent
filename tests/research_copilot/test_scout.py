from __future__ import annotations

import json
from pathlib import Path


def _paths(tmp_path: Path) -> dict[str, Path]:
    data = tmp_path / "research-copilot"
    data.mkdir()
    paths = {
        "data": data,
        "database": data / "library.db",
        "topics": data / "topics.yaml",
        "profile": data / "research-profile.yaml",
        "catalog": data / "sources.yaml",
    }
    paths["topics"].write_text("topics: []\n")
    paths["profile"].write_text("schema_version: 1\n")
    paths["catalog"].write_text("schema_version: 1\nsources: []\n")
    return paths


def test_codex_scout_uses_read_only_ephemeral_structured_execution(tmp_path, monkeypatch):
    from research_copilot import scout

    payload = {
        "summary": "one discovery",
        "candidates": [{
            "title": "Agent Memory",
            "url": "https://example.org/paper",
            "item_type": "paper",
            "topic_ids": ["long-horizon-agent"],
            "why_relevant": "Studies persistent memory.",
            "confidence": 0.8,
            "evidence_urls": ["https://example.org/paper"],
        }],
        "term_suggestions": ["persistent execution"],
        "source_suggestions": [],
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps(payload))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(scout.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(scout.subprocess, "run", fake_run)

    result = scout.run_codex_scout(paths=_paths(tmp_path), timeout_seconds=120)

    command, kwargs = calls[0]
    assert command[:2] == ["/usr/bin/codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in command
    assert "--output-schema" in command
    assert "software engineering tasks" in command[-1]
    assert "natural Chinese" in command[-1]
    assert "current beliefs" in command[-1]
    assert kwargs["timeout"] == 120
    assert result.payload == payload


def test_codex_scout_rejects_non_http_evidence(tmp_path, monkeypatch):
    from research_copilot import scout

    payload = {
        "summary": "bad",
        "candidates": [{
            "title": "Bad", "url": "https://example.org", "item_type": "paper",
            "topic_ids": [], "why_relevant": "bad", "confidence": .5,
            "evidence_urls": ["file:///tmp/fake"],
        }],
        "term_suggestions": [], "source_suggestions": [],
    }

    def fake_run(command, **_kwargs):
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(payload))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(scout.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(scout.subprocess, "run", fake_run)

    try:
        scout.run_codex_scout(paths=_paths(tmp_path))
    except ValueError as exc:
        assert "evidence URL" in str(exc)
    else:
        raise AssertionError("invalid evidence URL was accepted")


def test_save_scout_result_writes_profile_staging(tmp_path):
    from research_copilot.scout import save_scout_result

    payload = {"summary": "ok", "candidates": [], "term_suggestions": [], "source_suggestions": []}
    destination = save_scout_result(tmp_path, payload)

    assert destination.parent == tmp_path / "scout"
    assert json.loads(destination.read_text()) == payload
