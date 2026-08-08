"""Tests for the Obsidian vault export pipeline (research_copilot.export_obsidian)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from research_copilot.export_obsidian import (
    LIBRARY_SUBDIR,
    _extract_arxiv_id,
    _match_analysis,
    _parse_report_analyses,
    _slug,
    export,
)
from research_copilot.library import connect_library, initialize_library


def _seed_db(db_path: Path) -> None:
    conn = connect_library(db_path)
    initialize_library(conn, migrated_at="2026-08-01T00:00:00+00:00")
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    conn.execute(
        """INSERT INTO sources(id, provider, display_name, source_type, tier,
           enabled, config_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("huggingface-daily", "huggingface", "HuggingFace Daily", "api",
         0.9, 1, "{}", "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
    )
    # A paper with topics + recommendation
    conn.execute(
        """INSERT INTO research_items(id, canonical_key, item_type, title, summary,
           url, authors_json, published_at, first_discovered_at, last_seen_at,
           status, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "ri_test_1", "arxiv:2607.00482", "paper",
            "Know When to Stop: Segment-Level Credit Assignment",
            "Reasoning models overthink; segment-level credit assignment reduces this.",
            "https://huggingface.co/papers/2607.00482",
            json.dumps(["Alice", "Bob"]),
            "2026-08-04T00:00:00.000Z", "2026-08-05T10:00:00+00:00",
            "2026-08-05T10:00:00+00:00", "discovered", "{}",
        ),
    )
    conn.execute("INSERT INTO item_topics(item_id, topic_id, confidence) VALUES (?, ?, ?)",
                 ("ri_test_1", "rl-post-train", 0.9))
    conn.execute(
        "INSERT INTO item_sources(item_id, source_id, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, ?)",
        ("ri_test_1", "huggingface-daily", "2026-08-05T10:00:00+00:00",
         "2026-08-05T10:00:00+00:00"),
    )
    conn.execute(
        """INSERT INTO recommendations(id, item_id, recommended_at, score,
           score_breakdown_json, rationale) VALUES (?, ?, ?, ?, ?, ?)""",
        ("rec_test_1", "ri_test_1", "2026-08-06T00:30:00+00:00", 0.73, "{}",
         "Directly addresses overthinking in reasoning agents."),
    )
    # A news item
    conn.execute(
        """INSERT INTO research_items(id, canonical_key, item_type, title, summary,
           url, authors_json, published_at, first_discovered_at, last_seen_at,
           status, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "ri_test_2", "news:gpt-5-6-sol", "research_news",
            "GPT-5.6 Sol Uses Twice the Tokens of GPT-5.5",
            "Token economics for long-context agents are shifting.",
            "https://www.vincentschmalbach.com/gpt-5-6-sol",
            "[]", "2026-08-03T00:00:00.000Z", "2026-08-05T10:00:00+00:00",
            "2026-08-05T10:00:00+00:00", "discovered", "{}",
        ),
    )
    conn.commit()
    conn.close()


def _write_report(reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    p = reports_dir / "weekly-20260804.md"
    p.write_text(
        "📚 深度科研周报 · 2026-08-04\n\n"
        "**1. Know When to Stop: Segment-Level Credit Assignment**（EMNLP 2026）\n"
        "背景：reasoning 模型过度思考浪费 token。\n"
        "现象：长链推理中早期段错误难以定位。\n"
        "观点：段级 credit assignment 优于 token 级。\n"
        "方法：segment-level 奖励分配 + early stopping 策略。\n"
        "实验设计：4 个 reasoning 基准 + 消融。\n"
        "观察结论：token 减少 30%，准确率持平。\n"
        "局限：仅覆盖数学推理。\n",
        encoding="utf-8",
    )
    return p


def test_slug_and_arxiv_id() -> None:
    assert _slug("Know When to Stop: A/B Test!") == "Know-When-to-Stop-A-B-Test"
    assert _extract_arxiv_id({"canonical_key": "arxiv:2607.00482", "url": ""}) == "2607.00482"
    assert _extract_arxiv_id({"canonical_key": "hf:xyz",
                              "url": "https://huggingface.co/papers/2608.0001"}) == "2608.0001"


def test_export_creates_notes_and_index(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    reports = tmp_path / "reports"
    _seed_db(db)
    _write_report(reports)

    stats = export(tmp_path / "vault", db, reports)

    assert stats["papers"] == 1
    assert stats["news"] == 1
    papers_dir = tmp_path / "vault" / LIBRARY_SUBDIR / "01 - Papers"
    news_dir = tmp_path / "vault" / LIBRARY_SUBDIR / "02 - News"
    note = next(papers_dir.glob("*.md"))
    text = note.read_text(encoding="utf-8")
    # frontmatter
    assert "arxiv_id: \"2607.00482\"" in text
    assert "score: 0.73" in text
    assert "rl-post-train" in text
    # seven-part analysis backfilled from the report
    assert "**背景**：reasoning 模型过度思考浪费 token。" in text
    assert "**观察结论**：token 减少 30%，准确率持平。" in text
    # summary + url
    assert "Reasoning models overthink" in text
    assert "huggingface.co/papers/2607.00482" in text
    # news note
    assert len(list(news_dir.glob("*.md"))) == 1
    # index
    index = (tmp_path / "vault" / LIBRARY_SUBDIR / "00 - Index.md").read_text(encoding="utf-8")
    assert "Know When to Stop" in index
    assert "rl-post-train" in index


def test_export_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    reports = tmp_path / "reports"
    _seed_db(db)
    _write_report(reports)

    vault = tmp_path / "vault"
    stats1 = export(vault, db, reports)
    stats2 = export(vault, db, reports)

    assert stats1["created"] == 2  # paper + news
    assert stats2["created"] == 0
    assert stats2["updated"] == 0
    assert len(list((vault / LIBRARY_SUBDIR / "01 - Papers").glob("*.md"))) == 1


def test_parse_report_analyses_and_match(tmp_path: Path) -> None:
    report = _write_report(tmp_path / "reports")
    analyses = _parse_report_analyses(report.parent)
    assert len(analyses) == 1
    assert analyses[0]["title"].startswith("Know When to Stop")
    assert set(analyses[0]["parts"]) >= {"背景", "现象", "观点", "方法", "实验设计", "观察结论", "局限"}

    parts = _match_analysis("Know When to Stop: Segment-Level Credit Assignment",
                            analyses)
    assert parts is not None
    assert parts["观察结论"].startswith("token 减少")


def test_report_archive_copied(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    reports = tmp_path / "reports"
    _seed_db(db)
    _write_report(reports)
    stats = export(tmp_path / "vault", db, reports)
    assert stats["reports_archived"] == 1
    archived = tmp_path / "vault" / LIBRARY_SUBDIR / "03 - Weekly Reports" / "weekly-20260804.md"
    assert archived.exists()
