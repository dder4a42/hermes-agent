from __future__ import annotations

import json
from pathlib import Path

import pytest


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


def _fake_urlopen(payload: dict, monkeypatch):
    """Patch urllib.request.urlopen with a canned JSON response."""
    from research_copilot import scout

    body = json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}).encode("utf-8")

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return body

    calls = []
    monkeypatch.setattr(scout.urllib.request, "urlopen", lambda request, timeout: calls.append(request) or _Response())
    return calls


def test_deepseek_scout_posts_structured_json_and_validates(tmp_path, monkeypatch):
    from research_copilot import scout

    payload = {
        "summary": "一次发现",
        "candidates": [{
            "title": "Agent Memory",
            "url": "https://example.org/paper",
            "item_type": "paper",
            "topic_ids": ["long-horizon-agent"],
            "why_relevant": "研究持久化记忆。",
            "confidence": 0.8,
            "evidence_urls": ["https://example.org/paper"],
        }],
        "term_suggestions": ["persistent execution"],
        "source_suggestions": [],
    }
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls = _fake_urlopen(payload, monkeypatch)

    result = scout.run_deepseek_scout(paths=_paths(tmp_path), timeout_seconds=120)

    request = calls[0]
    assert request.full_url.endswith("/chat/completions")
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer test-key"
    body = json.loads(request.data)
    assert body["model"] == "deepseek-v4-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert "natural Chinese" in body["messages"][1]["content"]
    assert "current beliefs" in body["messages"][1]["content"]
    assert "output_schema" in body["messages"][1]["content"]
    assert result.payload == payload


def test_deepseek_scout_model_override(tmp_path, monkeypatch):
    from research_copilot import scout

    payload = {"summary": "ok", "candidates": [], "term_suggestions": [], "source_suggestions": []}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("RESEARCH_COPILOT_SCOUT_MODEL", "deepseek-chat")
    calls = _fake_urlopen(payload, monkeypatch)

    scout.run_deepseek_scout(paths=_paths(tmp_path))

    body = json.loads(calls[0].data)
    assert body["model"] == "deepseek-chat"


def test_deepseek_scout_requires_api_key(tmp_path, monkeypatch):
    from research_copilot import scout

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        scout.run_deepseek_scout(paths=_paths(tmp_path))


def test_deepseek_scout_rejects_non_http_evidence(tmp_path, monkeypatch):
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
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    _fake_urlopen(payload, monkeypatch)

    with pytest.raises(ValueError, match="evidence URL"):
        scout.run_deepseek_scout(paths=_paths(tmp_path))


def test_save_scout_result_writes_profile_staging(tmp_path):
    from research_copilot.scout import save_scout_result

    payload = {"summary": "ok", "candidates": [], "term_suggestions": [], "source_suggestions": []}
    destination = save_scout_result(tmp_path, payload)

    assert destination.parent == tmp_path / "scout"
    assert json.loads(destination.read_text()) == payload


def test_newsletter_news_highlights_only_resolved_news_types(tmp_path):
    from datetime import datetime, timedelta, timezone

    import sqlite3

    from research_copilot.scout import newsletter_news_highlights

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE newsletter_issues (id INTEGER PRIMARY KEY, created_at TEXT)")
    conn.execute(
        """CREATE TABLE newsletter_entries (
            id INTEGER PRIMARY KEY, issue_id INTEGER, title TEXT, canonical_url TEXT,
            content_type TEXT, excerpt TEXT, resolved_at TEXT,
            filter_reason TEXT, resolution_status TEXT)"""
    )
    now = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn.execute("INSERT INTO newsletter_issues (id, created_at) VALUES (1, ?)", (now,))
    entries = [
        (1, "Fresh AI news", "https://news.example/1", "research_news", "hot", now, "", "resolved"),
        (2, "Old news", "https://news.example/2", "research_news", "stale", old, "", "resolved"),
        (3, "Filtered out", "https://news.example/3", "research_news", "x", now, "advertisement", "resolved"),
        (4, "Failed URL", "https://news.example/4", "research_news", "x", now, "", "failed"),
        (5, "Paper stays in main pipeline", "https://arxiv.org/abs/2608.00001", "paper", "x", now, "", "resolved"),
        (6, "Engineering item", "https://news.example/6", "engineering", "x", now, "", "resolved"),
        (7, "Product release", "https://news.example/7", "product_release", "x", now, "", "resolved"),
    ]
    conn.executemany(
        """INSERT INTO newsletter_entries (id, issue_id, title, canonical_url, content_type,
           excerpt, resolved_at, filter_reason, resolution_status) VALUES (?,1,?,?,?,?,?,?,?)""",
        entries,
    )
    result = newsletter_news_highlights(conn, days=7, limit=10)
    titles = [item["title"] for item in result]
    assert "Fresh AI news" in titles
    assert "Product release" in titles
    assert "Old news" not in titles          # outside the window
    assert "Filtered out" not in titles      # filter_reason set
    assert "Failed URL" not in titles        # not resolved
    assert "Paper stays in main pipeline" not in titles  # paper goes through the main pipeline
    assert "Engineering item" not in titles  # engineering goes through the main pipeline
    assert all(item["url"] for item in result)
    conn.close()
