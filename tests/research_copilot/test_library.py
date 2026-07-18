from __future__ import annotations

from datetime import datetime, timezone

import pytest

from research_copilot.library import (
    LibraryRepository,
    ResearchItemDraft,
    SourceEvidence,
    SourceRunCounts,
    TopicMatch,
    connect_library,
    initialize_library,
)
from research_copilot.library.identity import canonical_key, normalize_arxiv_id, normalize_url


@pytest.fixture
def library(tmp_path):
    connection = connect_library(tmp_path / "library.db")
    initialize_library(connection, migrated_at="2026-07-17T00:00:00+00:00")
    try:
        yield connection, LibraryRepository(connection)
    finally:
        connection.close()


def test_connection_enforces_library_pragmas(library):
    connection, _ = library
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_identity_normalizes_arxiv_versions_and_tracking_urls():
    assert normalize_arxiv_id("https://arxiv.org/pdf/2607.01234v3.pdf") == "2607.01234"
    assert normalize_url("HTTPS://www.Example.com/paper/?utm_source=x&b=2&a=1#top") == "https://example.com/paper?a=1&b=2"
    assert canonical_key(ResearchItemDraft(title="ignored", doi="https://doi.org/10.1/ABC")) == "doi:10.1/abc"


def test_upsert_merges_sources_topics_and_better_metadata(library):
    connection, repo = library
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    repo.upsert_source(source_id="hf", provider="huggingface", display_name="HF", source_type="curated", tier=1.0, now=now)
    repo.upsert_source(source_id="s2", provider="semantic_scholar", display_name="S2", source_type="index", tier=0.85, now=now)

    first = repo.upsert_item(
        ResearchItemDraft(title="A Great Paper", arxiv_id="2607.01234", summary="short"),
        source=SourceEvidence("hf"),
        topics=(TopicMatch("research-agent", 0.6, ("agent",)),),
        discovered_at=now,
    )
    second = repo.upsert_item(
        ResearchItemDraft(
            title="A Great Paper",
            arxiv_id="2607.01234v2",
            summary="a substantially more complete abstract",
            authors=("Alice", "Bob"),
        ),
        source=SourceEvidence("s2", query="research agent", rank=1),
        topics=(
            TopicMatch("research-agent", 0.9, ("research agent",)),
            TopicMatch("long-horizon-agent", 0.7),
        ),
        discovered_at=now,
    )

    assert first.disposition == "new"
    assert second.disposition == "merged"
    assert first.item_id == second.item_id
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM item_sources").fetchone()[0] == 2
    assert connection.execute("SELECT count(*) FROM item_topics").fetchone()[0] == 2
    item = connection.execute("SELECT * FROM research_items").fetchone()
    assert item["summary"] == "a substantially more complete abstract"
    topic = connection.execute("SELECT * FROM item_topics WHERE topic_id='research-agent'").fetchone()
    assert topic["confidence"] == 0.9


def test_upsert_rolls_back_when_source_is_unknown(library):
    connection, repo = library
    with pytest.raises(Exception):
        repo.upsert_item(
            ResearchItemDraft(title="Uncommitted", url="https://example.com/p"),
            source=SourceEvidence("missing"),
            discovered_at=datetime.now(timezone.utc),
        )
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 0


def test_new_stronger_identifier_merges_through_existing_url(library):
    connection, repo = library
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    repo.upsert_source(source_id="rss", provider="rss", display_name="RSS", source_type="feed", tier=0.8, now=now)
    repo.upsert_source(source_id="crossref", provider="crossref", display_name="Crossref", source_type="index", tier=0.9, now=now)

    first = repo.upsert_item(
        ResearchItemDraft(title="Paper", url="https://example.com/paper?utm_source=feed"),
        source=SourceEvidence("rss"), discovered_at=now,
    )
    second = repo.upsert_item(
        ResearchItemDraft(title="Paper", url="https://example.com/paper", doi="10.1/paper"),
        source=SourceEvidence("crossref"), discovered_at=now,
    )

    assert second.item_id == first.item_id
    assert second.canonical_key == first.canonical_key
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM item_identifiers").fetchone()[0] == 2


def test_naive_timestamps_are_rejected(library):
    _, repo = library
    with pytest.raises(ValueError, match="timezone-aware"):
        repo.upsert_source(
            source_id="x", provider="rss", display_name="X",
            source_type="feed", tier=0.5, now=datetime(2026, 7, 17),
        )


def _create_item(library):
    _, repo = library
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    repo.upsert_source(
        source_id="rss", provider="rss", display_name="RSS",
        source_type="feed", tier=0.8, now=now,
    )
    result = repo.upsert_item(
        ResearchItemDraft(title="A paper", url="https://example.com/paper"),
        source=SourceEvidence("rss"), discovered_at=now,
    )
    return result.item_id, now


def test_source_run_has_one_terminal_transition(library):
    connection, repo = library
    _, now = _create_item(library)
    run_id = repo.start_source_run(source_id="rss", started_at=now, metrics={"catalog_hash": "abc"})
    repo.finish_source_run(
        run_id, status="partial", finished_at=now,
        counts=SourceRunCounts(requests=2, fetched=8, new=3, merged=2, unchanged=1, filtered=2),
        error_code="rate_limited", error_message="HTTP 429",
    )
    row = connection.execute("SELECT * FROM source_runs WHERE id = ?", (run_id,)).fetchone()
    assert row["status"] == "partial"
    assert (row["new_count"], row["merged_count"], row["filtered_count"]) == (3, 2, 2)
    with pytest.raises(ValueError, match="already finished"):
        repo.finish_source_run(run_id, status="success", finished_at=now)


def test_source_run_rejects_negative_counts():
    with pytest.raises(ValueError, match="cannot be negative"):
        SourceRunCounts(fetched=-1)


def test_recommendation_and_status_change_are_atomic(library):
    connection, repo = library
    item_id, now = _create_item(library)
    rec_id = repo.record_recommendation(
        item_id, score=0.82, score_breakdown={"relevance": 0.9},
        recommended_at=now, rationale="Strong match",
    )
    assert rec_id.startswith("rec_")
    assert connection.execute("SELECT status FROM research_items WHERE id = ?", (item_id,)).fetchone()[0] == "recommended"
    assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1

    with pytest.raises(Exception):
        repo.record_recommendation(
            item_id, score=1.5, score_breakdown={}, recommended_at=now,
        )
    assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1
    assert connection.execute("SELECT status FROM research_items WHERE id = ?", (item_id,)).fetchone()[0] == "recommended"


def test_failed_recommendation_does_not_change_item_status(library):
    connection, repo = library
    item_id, now = _create_item(library)
    with pytest.raises(Exception):
        repo.record_recommendation(
            item_id, score=2.0, score_breakdown={}, recommended_at=now,
        )
    assert connection.execute("SELECT status FROM research_items WHERE id = ?", (item_id,)).fetchone()[0] == "discovered"
    assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 0


def test_feedback_and_status_change_are_atomic(library):
    connection, repo = library
    item_id, now = _create_item(library)
    event_id = repo.record_feedback(
        item_id, kind="save", created_at=now, payload={"reason": "read later"},
    )
    assert event_id.startswith("fb_")
    assert connection.execute("SELECT status FROM research_items WHERE id = ?", (item_id,)).fetchone()[0] == "saved"
    assert connection.execute("SELECT kind FROM feedback_events").fetchone()[0] == "save"

    repo.record_feedback(item_id, kind="note", created_at=now, payload={"text": "important"})
    assert connection.execute("SELECT status FROM research_items WHERE id = ?", (item_id,)).fetchone()[0] == "saved"


def test_feedback_for_unknown_item_does_not_write_event(library):
    connection, repo = library
    with pytest.raises(ValueError, match="Unknown Research Item"):
        repo.record_feedback(
            "missing", kind="save", created_at=datetime.now(timezone.utc),
        )
    assert connection.execute("SELECT count(*) FROM feedback_events").fetchone()[0] == 0


def test_classify_item_is_read_only_and_detects_source_merge(library):
    connection, repo = library
    item_id, _ = _create_item(library)
    draft = ResearchItemDraft(title="A paper", url="https://example.com/paper")
    assert repo.classify_item(draft, source_id="rss") == "unchanged"
    repo.upsert_source(
        source_id="other", provider="rss", display_name="Other",
        source_type="feed", tier=0.7, now=datetime.now(timezone.utc),
    )
    assert repo.classify_item(draft, source_id="other") == "merged"
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM item_sources WHERE item_id = ?", (item_id,)).fetchone()[0] == 1
