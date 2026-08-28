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
        "research_config": data / "research-config.yaml",
        "catalog": data / "sources.yaml",
    }
    paths["topics"].write_text(
        "topics:\n"
        "  - id: long-horizon-agent\n"
        "    name: Long-horizon agents\n"
        "    status: active\n"
    )
    paths["research_config"].write_text(
        "schema_version: 2\nlong_term_agenda: []\nevidence_ledger: []\n"
    )
    paths["catalog"].write_text("schema_version: 1\nsources: []\n")
    return paths


def test_hermes_scout_uses_isolated_profile_and_web_toolset(tmp_path, monkeypatch):
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

    def fake_run(command, cwd, timeout_seconds):
        calls.append((command, cwd, timeout_seconds))
        return type(
            "Result", (),
            {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""},
        )()

    monkeypatch.setattr(scout.shutil, "which", lambda name: "/usr/bin/hermes")

    paths = _paths(tmp_path)
    result = scout.run_hermes_scout(
        paths=paths, timeout_seconds=120, command_runner=fake_run,
    )

    command, cwd, timeout_seconds = calls[0]
    assert command[:3] == ["/usr/bin/hermes", "-p", "research-copilot"]
    assert command[-3:] == ["--json-output", "-t", "web"]
    prompt = command[command.index("-z") + 1]
    assert "Do not delegate" in prompt
    assert "natural Chinese" in prompt
    assert '"research_config_yaml"' in prompt
    assert cwd == paths["data"]
    assert timeout_seconds == 120
    assert result.payload == payload


def test_hermes_scout_rejects_non_http_evidence(tmp_path, monkeypatch):
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

    def fake_run(_command, _cwd, _timeout_seconds):
        return type(
            "Result", (),
            {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""},
        )()

    monkeypatch.setattr(scout.shutil, "which", lambda name: "/usr/bin/hermes")

    try:
        scout.run_hermes_scout(paths=_paths(tmp_path), command_runner=fake_run)
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
