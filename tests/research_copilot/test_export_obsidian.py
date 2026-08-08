"""Tests for the Obsidian vault export pipeline (research_copilot.export_obsidian)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from research_copilot.export_obsidian import (
    LIBRARY_SUBDIR,
    _extract_arxiv_id,
    _date,
    _match_analysis,
    _parse_report_analyses,
    _published_report_text,
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
           workflow_state, metadata_json)
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
           workflow_state, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
    assert _date("Fri, 03 Jul 2026 11:00:00 +0000") == "2026-07-03"
    assert _date("not-a-date") == ""


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
    assert 'agent_analysis_status: "none"' in text
    assert 'user_learning_status: "unseen"' in text
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


def test_export_retires_legacy_slug_duplicate_after_preserving_curated_sections(
    tmp_path: Path,
) -> None:
    db = tmp_path / "library.db"
    _seed_db(db)
    vault = tmp_path / "vault"
    export(vault, db)
    news_dir = vault / LIBRARY_SUBDIR / "02 - News"
    current = next(news_dir.glob("*.md"))
    legacy = news_dir / "2026-08-03 - GPT-5-6-Sol-Uses-Twice.md"
    text = current.read_text(encoding="utf-8")
    text = text.replace('canonical_key: "news:gpt-5-6-sol"\n', "")
    text = text.replace("**背景**：", "**背景**：人工保留内容", 1)
    text = text.replace(
        "<!-- 手动添加 [[wikilinks]] -->", "[[related-note]]",
    )
    legacy.write_text(text, encoding="utf-8")

    stats = export(vault, db)

    assert stats["retired_duplicates"] == 1
    assert not legacy.exists()
    refreshed = current.read_text(encoding="utf-8")
    assert "**背景**：人工保留内容" in refreshed
    assert "[[related-note]]" in refreshed


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

    # A subsequent idempotent run still reports the total archive in the MOC.
    export(tmp_path / "vault", db, reports)
    index = (tmp_path / "vault" / LIBRARY_SUBDIR / "00 - Index.md").read_text(encoding="utf-8")
    assert "周报归档：1" in index
    assert "[[weekly-20260804]]" in index


def test_report_archive_keeps_only_final_article_from_cli_transcript(tmp_path: Path) -> None:
    raw = (
        "Query: write report\nInitializing agent\nReasoning\n"
        "📚 深度科研周报 · 2026-08-04\nDRAFT\nSession: draft\n"
        "📚 深度科研周报 · 2026-08-04\nFINAL ARTICLE\n"
        "Resume this session with id secret-session\n"
    )
    assert _published_report_text(raw) == (
        "📚 深度科研周报 · 2026-08-04\nFINAL ARTICLE\n"
    )

    db = tmp_path / "library.db"
    reports = tmp_path / "reports"
    _seed_db(db)
    reports.mkdir()
    (reports / "weekly-20260804.md").write_text(raw, encoding="utf-8")
    export(tmp_path / "vault", db, reports)
    archived = (
        tmp_path / "vault" / LIBRARY_SUBDIR / "03 - Weekly Reports" / "weekly-20260804.md"
    ).read_text(encoding="utf-8")
    assert "FINAL ARTICLE" in archived
    assert "Query:" not in archived
    assert "secret-session" not in archived


def test_export_uses_resolvable_links_and_preserves_curated_sections(tmp_path: Path) -> None:
    from research_copilot.wiki import audit_wiki

    db = tmp_path / "library.db"
    reports = tmp_path / "reports"
    _seed_db(db)
    _write_report(reports)
    vault = tmp_path / "vault"
    export(vault, db, reports)

    library = vault / LIBRARY_SUBDIR
    note = next((library / "01 - Papers").glob("*.md"))
    original = note.read_text(encoding="utf-8")
    assert f"[[{note.stem}|Know When to Stop: Segment-Level Credit Assignment]]" in (
        library / "00 - Index.md"
    ).read_text(encoding="utf-8")
    assert audit_wiki(library).errors == 0

    curated = original.replace(
        "**背景**：reasoning 模型过度思考浪费 token。",
        "**背景**：curator-owned analysis",
    ).replace(
        "<!-- 手动添加 [[wikilinks]] -->",
        "[[rl-post-training]]",
    )
    note.write_text(curated, encoding="utf-8")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE research_items SET summary=? WHERE id=?", ("Updated generated summary.", "ri_test_1"))
    conn.commit()
    conn.close()

    export(vault, db, reports, full=True)
    refreshed = note.read_text(encoding="utf-8")
    assert "Updated generated summary." in refreshed
    assert "**背景**：curator-owned analysis" in refreshed
    assert "[[rl-post-training]]" in refreshed
    assert 'aliases: ["Know When to Stop: Segment-Level Credit Assignment"]' in refreshed
    assert audit_wiki(library).errors == 1  # the intentionally missing curator link


def test_export_preserves_analysis_written_on_line_after_label(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    _seed_db(db)
    vault = tmp_path / "vault"
    export(vault, db)
    note = next((vault / LIBRARY_SUBDIR / "01 - Papers").glob("*.md"))
    text = note.read_text(encoding="utf-8")
    text = text.replace("**背景**：", "**背景**：\n这是多行形式的人工解读。", 1)
    note.write_text(text, encoding="utf-8")

    export(vault, db, full=True)

    assert "**背景**：\n这是多行形式的人工解读。" in note.read_text(encoding="utf-8")


def test_recommendation_after_empty_limitations_is_not_curated_analysis(tmp_path: Path) -> None:
    from research_copilot.export_obsidian import _has_analysis

    db = tmp_path / "library.db"
    _seed_db(db)
    vault = tmp_path / "vault"
    export(vault, db)
    note = next((vault / LIBRARY_SUBDIR / "01 - Papers").glob("*.md"))

    assert "**推荐理由**：" in note.read_text(encoding="utf-8")
    assert _has_analysis(note) is False


def test_export_merges_library_records_that_map_to_the_same_note(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    _seed_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT INTO research_items(id, canonical_key, item_type, title, summary,
           url, authors_json, published_at, first_discovered_at, last_seen_at,
           workflow_state, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "ri_project_page", "url:https://example.test/project", "paper",
            "Know When to Stop: Segment-Level Credit Assignment", "Project page summary",
            "https://example.test/project", "[]", "2026-08-04T00:00:00Z",
            "2026-08-05T11:00:00Z", "2026-08-05T11:00:00Z", "discovered", "{}",
        ),
    )
    conn.execute(
        "INSERT INTO item_sources(item_id, source_id, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?)",
        ("ri_project_page", "huggingface-daily", "2026-08-05T11:00:00Z", "2026-08-05T11:00:00Z"),
    )
    conn.commit()
    conn.close()

    stats = export(tmp_path / "vault", db)
    notes = list((tmp_path / "vault" / LIBRARY_SUBDIR / "01 - Papers").glob("*.md"))
    assert len(notes) == 1
    assert stats["merged_records"] == 1
    text = notes[0].read_text(encoding="utf-8")
    assert "arxiv:2607.00482" in text
    assert "url:https://example.test/project" in text


def test_export_disambiguates_distinct_titles_with_the_same_slug(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    _seed_db(db)
    conn = sqlite3.connect(db)
    rows = []
    for item_id, canonical_key, title in (
        ("ri_collision_a", "url:https://example.test/a", "A/B"),
        ("ri_collision_b", "url:https://example.test/b", "A B"),
    ):
        rows.append((
            item_id, canonical_key, "paper", title, "", canonical_key.removeprefix("url:"), "[]",
            "2026-08-04T00:00:00Z", "2026-08-05T11:00:00Z", "2026-08-05T11:00:00Z",
            "discovered", "{}",
        ))
    conn.executemany(
        """INSERT INTO research_items(id, canonical_key, item_type, title, summary,
           url, authors_json, published_at, first_discovered_at, last_seen_at,
           workflow_state, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()

    export(tmp_path / "vault", db)
    notes = list((tmp_path / "vault" / LIBRARY_SUBDIR / "01 - Papers").glob("2026-08-04 - A-B-*.md"))
    assert len(notes) == 2
    assert len({path.name for path in notes}) == 2
