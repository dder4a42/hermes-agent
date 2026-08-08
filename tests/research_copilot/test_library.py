from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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
from research_copilot.library.database import SCHEMA_VERSION


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


def test_schema_v1_migrates_status_and_recommendations_without_data_loss(tmp_path):
    database = tmp_path / "legacy.db"
    connection = connect_library(database)
    connection.executescript("""
        CREATE TABLE schema_metadata (
            singleton INTEGER PRIMARY KEY, schema_version INTEGER NOT NULL, migrated_at TEXT NOT NULL
        );
        INSERT INTO schema_metadata VALUES (1, 1, '2026-07-17T00:00:00+00:00');
        CREATE TABLE research_items (
            id TEXT PRIMARY KEY, canonical_key TEXT NOT NULL UNIQUE,
            item_type TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '', authors_json TEXT NOT NULL DEFAULT '[]',
            published_at TEXT, first_discovered_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'discovered', metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        INSERT INTO research_items VALUES (
            'one', 'url:https://example.com/one', 'paper', 'One', '', '', '[]', NULL,
            '2026-07-17T00:00:00+00:00', '2026-07-17T00:00:00+00:00', 'recommended', '{}'
        );
        INSERT INTO research_items VALUES (
            'two', 'url:https://example.com/two', 'paper', 'Two', '', '', '[]', NULL,
            '2026-07-17T00:00:00+00:00', '2026-07-17T00:00:00+00:00', 'saved', '{}'
        );
        CREATE TABLE recommendations (
            id TEXT PRIMARY KEY, item_id TEXT NOT NULL UNIQUE REFERENCES research_items(id),
            recommended_at TEXT NOT NULL, score REAL NOT NULL,
            score_breakdown_json TEXT NOT NULL, rationale TEXT NOT NULL DEFAULT ''
        );
        INSERT INTO recommendations VALUES (
            'rec_old', 'one', '2026-07-17T00:00:00+00:00', 0.8, '{}', 'legacy'
        );
    """)
    connection.close()
    from research_copilot.runtime import open_library

    connection, repo = open_library(database)
    try:
        backups = list((tmp_path / "backups").glob("library-schema-v1-*.db"))
        assert len(backups) == 1
        assert connection.execute("SELECT schema_version FROM schema_metadata").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_runtime_state'"
        ).fetchone() is not None
        states = dict(connection.execute("SELECT id,workflow_state FROM research_items"))
        assert states == {"one": "discovered", "two": "shortlisted"}
        assert dict(connection.execute(
            "SELECT id,normalized_title FROM research_items"
        )) == {"one": "one", "two": "two"}
        assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1
        repo.record_recommendation(
            "one", score=0.7, score_breakdown={},
            recommended_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )
        assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 2
    finally:
        connection.close()


def test_schema_v2_is_backed_up_before_incremental_state_migration(tmp_path):
    database = tmp_path / "library.db"
    connection = connect_library(database)
    initialize_library(connection, migrated_at="2026-07-17T00:00:00+00:00")
    with connection:
        connection.execute("UPDATE schema_metadata SET schema_version = 2")
        connection.execute("DROP TABLE source_runtime_state")
    connection.close()

    from research_copilot.runtime import open_library

    connection, _ = open_library(database)
    try:
        assert len(list((tmp_path / "backups").glob("library-schema-v2-*.db"))) == 1
        assert connection.execute("SELECT schema_version FROM schema_metadata").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_runtime_state'"
        ).fetchone() is not None
    finally:
        connection.close()


def test_schema_v3_is_backed_up_before_evidence_ledger_migration(tmp_path):
    database = tmp_path / "library.db"
    connection = connect_library(database)
    initialize_library(connection, migrated_at="2026-08-08T00:00:00+00:00")
    with connection:
        connection.execute("UPDATE schema_metadata SET schema_version = 3")
        connection.execute("DROP TABLE evidence_records")
    connection.close()

    from research_copilot.runtime import open_library

    connection, _ = open_library(database)
    try:
        assert len(list((tmp_path / "backups").glob("library-schema-v3-*.db"))) == 1
        assert connection.execute("SELECT schema_version FROM schema_metadata").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_records'"
        ).fetchone() is not None
    finally:
        connection.close()


def test_schema_v4_migrates_independent_analysis_and_learning_states(tmp_path):
    database = tmp_path / "library.db"
    connection = connect_library(database)
    connection.executescript("""
        CREATE TABLE schema_metadata (
            singleton INTEGER PRIMARY KEY, schema_version INTEGER NOT NULL, migrated_at TEXT NOT NULL
        );
        INSERT INTO schema_metadata VALUES (1, 4, '2026-08-08T00:00:00+00:00');
        CREATE TABLE research_items (
            id TEXT PRIMARY KEY, canonical_key TEXT NOT NULL UNIQUE,
            item_type TEXT NOT NULL, title TEXT NOT NULL, normalized_title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
            authors_json TEXT NOT NULL DEFAULT '[]', published_at TEXT,
            first_discovered_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            workflow_state TEXT NOT NULL DEFAULT 'discovered',
            starred INTEGER NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        INSERT INTO research_items VALUES
            ('saved', 'url:saved', 'paper', 'Saved', '', '', '', '[]', NULL, 't', 't', 'shortlisted', 0, '{}'),
            ('done', 'url:done', 'paper', 'Done', '', '', '', '[]', NULL, 't', 't', 'synthesized', 0, '{}');
    """)
    initialize_library(connection, migrated_at="2026-08-09T00:00:00+00:00")
    rows = {
        row["id"]: (row["agent_analysis_status"], row["user_learning_status"])
        for row in connection.execute("SELECT * FROM research_items")
    }
    assert rows == {"saved": ("none", "saved"), "done": ("synthesized", "read")}
    assert connection.execute("SELECT schema_version FROM schema_metadata").fetchone()[0] == SCHEMA_VERSION
    connection.close()


def test_schema_v5_is_backed_up_before_delivery_outbox_migration(tmp_path):
    database = tmp_path / "library.db"
    connection = connect_library(database)
    initialize_library(connection, migrated_at="2026-08-09T00:00:00+00:00")
    with connection:
        connection.execute("UPDATE schema_metadata SET schema_version=5")
        connection.execute("DROP TABLE recommendation_delivery_outbox")
    connection.close()

    from research_copilot.runtime import open_library

    connection, _ = open_library(database)
    try:
        assert len(list((tmp_path / "backups").glob("library-schema-v5-*.db"))) == 1
        assert connection.execute("SELECT schema_version FROM schema_metadata").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='recommendation_delivery_outbox'"
        ).fetchone() is not None
    finally:
        connection.close()


def test_schema_v6_is_backed_up_before_deep_research_outbox_migration(tmp_path):
    database = tmp_path / "library.db"
    connection = connect_library(database)
    initialize_library(connection, migrated_at="2026-08-09T00:00:00+00:00")
    with connection:
        connection.execute("UPDATE schema_metadata SET schema_version=6")
        connection.execute("DROP TABLE deep_research_delivery_outbox")
    connection.close()

    from research_copilot.runtime import open_library

    connection, _ = open_library(database)
    try:
        assert len(list((tmp_path / "backups").glob("library-schema-v6-*.db"))) == 1
        assert connection.execute("SELECT schema_version FROM schema_metadata").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='deep_research_delivery_outbox'"
        ).fetchone() is not None
    finally:
        connection.close()


def test_identity_normalizes_arxiv_versions_and_tracking_urls():
    assert normalize_arxiv_id("https://arxiv.org/pdf/2607.01234v3.pdf") == "2607.01234"
    assert normalize_url("HTTPS://www.Example.com/paper/?utm_source=x&b=2&a=1#top") == "https://example.com/paper?a=1&b=2"
    assert canonical_key(ResearchItemDraft(title="ignored", doi="https://doi.org/10.1/ABC")) == "doi:10.1/abc"
    assert normalize_arxiv_id("https://arxiv.org/abs/hep-th/9901001v2") == "hep-th/9901001"
    assert canonical_key(
        ResearchItemDraft(title="ignored", url="https://doi.org/10.1234/Paper.X"),
    ) == "doi:10.1234/paper.x"


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
    assert second.canonical_key == "doi:10.1/paper"
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM item_identifiers").fetchone()[0] == 2


def test_guarded_reconciliation_merges_project_page_with_arxiv_and_records_evidence(library):
    connection, repo = library
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    for source_id in ("project", "arxiv"):
        repo.upsert_source(
            source_id=source_id, provider=source_id, display_name=source_id,
            source_type="index", tier=0.8, now=now,
        )
    first = repo.upsert_item(
        ResearchItemDraft(
            title="Video Generation Models are General-Purpose Vision Learners",
            url="https://genception.example/", authors=("Ada Lovelace",),
            published_at="2026-07-01",
        ),
        source=SourceEvidence("project"), discovered_at=now,
    )
    second = repo.upsert_item(
        ResearchItemDraft(
            title="Video Generation Models are General Purpose Vision Learners",
            url="https://arxiv.org/abs/2607.09024", arxiv_id="2607.09024",
            authors=("Ada Lovelace", "Bob"), published_at="2026-07-02",
        ),
        source=SourceEvidence("arxiv"), discovered_at=now,
    )
    assert second.item_id == first.item_id
    assert second.canonical_key == "arxiv:2607.09024"
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 1
    evidence = connection.execute("SELECT * FROM item_merge_evidence").fetchone()
    assert evidence["method"] == "exact_title_with_author_or_year"
    assert "ada lovelace" in json.loads(evidence["evidence_json"])["author_overlap"]


def test_explicit_duplicate_merge_preserves_sources_topics_and_alternate_url(library):
    connection, repo = library
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    for source_id in ("project", "arxiv"):
        repo.upsert_source(
            source_id=source_id, provider=source_id, display_name=source_id,
            source_type="index", tier=0.8, now=now,
        )
    project = repo.upsert_item(
        ResearchItemDraft(
            title="One Research Work", item_type="engineering",
            url="https://project.example/",
        ),
        source=SourceEvidence("project"), topics=(TopicMatch("agent", .7),),
        discovered_at=now,
    )
    paper = repo.upsert_item(
        ResearchItemDraft(
            title="One Research Work", item_type="paper",
            url="https://arxiv.org/abs/2607.12345", arxiv_id="2607.12345",
        ),
        source=SourceEvidence("arxiv"), topics=(TopicMatch("agent", .9),),
        discovered_at=now,
    )

    repo.merge_items(
        source_item_id=project.item_id, target_item_id=paper.item_id,
        observed_at=now,
        reason="The primary paper links this exact project page.",
    )

    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 1
    sources = {
        row[0] for row in connection.execute(
            "SELECT source_id FROM item_sources WHERE item_id=?", (paper.item_id,),
        )
    }
    assert sources == {"project", "arxiv"}
    row = connection.execute(
        "SELECT metadata_json FROM research_items WHERE id=?", (paper.item_id,),
    ).fetchone()
    assert json.loads(row[0])["alternate_urls"] == ["https://project.example/"]
    evidence = connection.execute(
        "SELECT * FROM item_merge_evidence WHERE method='explicit_verified_duplicate'",
    ).fetchone()
    assert evidence["incoming_canonical_key"] == "url:https://project.example/"


def test_reconciliation_refuses_conflicting_strong_identifiers(library):
    connection, repo = library
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    repo.upsert_source(
        source_id="index", provider="index", display_name="Index",
        source_type="index", tier=0.8, now=now,
    )
    for doi in ("10.1234/one", "10.1234/two"):
        repo.upsert_item(
            ResearchItemDraft(
                title="A Shared Benchmark Title", doi=doi,
                authors=("Alice",), published_at="2026",
            ),
            source=SourceEvidence("index"), discovered_at=now,
        )
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 2
    assert connection.execute("SELECT count(*) FROM item_merge_evidence").fetchone()[0] == 0


def test_reconciliation_does_not_merge_on_title_alone(library):
    connection, repo = library
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    repo.upsert_source(
        source_id="web", provider="web", display_name="Web",
        source_type="web", tier=0.5, now=now,
    )
    for url in ("https://one.example/paper", "https://two.example/paper"):
        repo.upsert_item(
            ResearchItemDraft(title="A Shared Benchmark Title", url=url),
            source=SourceEvidence("web"), discovered_at=now,
        )
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 2


def test_reconciliation_requires_academic_compatible_item_types(library):
    connection, repo = library
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    repo.upsert_source(
        source_id="mixed", provider="mixed", display_name="Mixed",
        source_type="feed", tier=0.5, now=now,
    )
    repo.upsert_item(
        ResearchItemDraft(
            title="A Shared Research Announcement", item_type="research_news",
            authors=("Alice",), published_at="2026",
            url="https://news.example/announcement",
        ), source=SourceEvidence("mixed"), discovered_at=now,
    )
    repo.upsert_item(
        ResearchItemDraft(
            title="A Shared Research Announcement", item_type="paper",
            authors=("Alice",), published_at="2026", arxiv_id="2607.00001",
        ), source=SourceEvidence("mixed"), discovered_at=now,
    )
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 2


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


def test_recommendation_is_an_event_and_does_not_change_workflow_state(library):
    connection, repo = library
    item_id, now = _create_item(library)
    rec_id = repo.record_recommendation(
        item_id, score=0.82, score_breakdown={"relevance": 0.9},
        recommended_at=now, rationale="Strong match",
    )
    assert rec_id.startswith("rec_")
    assert connection.execute("SELECT workflow_state FROM research_items WHERE id = ?", (item_id,)).fetchone()[0] == "discovered"
    assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1

    second = repo.record_recommendation(
        item_id, score=0.7, score_breakdown={}, recommended_at=now,
    )
    assert second != rec_id
    assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 2
    assert connection.execute("SELECT workflow_state FROM research_items WHERE id = ?", (item_id,)).fetchone()[0] == "discovered"


def test_recommendation_delivery_outbox_retries_until_acknowledged(library):
    connection, repo = library
    item_id, now = _create_item(library)
    recommendation_id = repo.record_recommendation(
        item_id, score=0.82, score_breakdown={"relevance": 0.9},
        recommended_at=now, enqueue_delivery=True,
    )
    pending = repo.pending_delivery()
    assert pending["recommendation_id"] == recommendation_id
    assert pending["attempt_count"] == 0

    repo.set_delivery_payload(recommendation_id, payload={"kind": "test"})
    repo.mark_delivery_attempt(pending["id"], attempted_at=now)
    repo.reconcile_delivery_attempt(
        completed_at=now + timedelta(minutes=1), delivered=False,
        error="weixin rate limited",
    )
    pending = repo.pending_delivery()
    assert pending["payload"] == {"kind": "test"}
    assert pending["attempt_count"] == 1
    assert pending["last_error"] == "weixin rate limited"

    retry_at = now + timedelta(minutes=2)
    repo.mark_delivery_attempt(pending["id"], attempted_at=retry_at)
    repo.reconcile_delivery_attempt(
        completed_at=retry_at + timedelta(minutes=1), delivered=True,
    )
    assert repo.pending_delivery() is None
    delivered = connection.execute(
        "SELECT status,attempt_count FROM recommendation_delivery_outbox"
    ).fetchone()
    assert tuple(delivered) == ("delivered", 2)


def test_manual_recommendation_does_not_enter_delivery_outbox(library):
    connection, repo = library
    item_id, now = _create_item(library)
    repo.record_recommendation(
        item_id, score=0.7, score_breakdown={}, recommended_at=now,
    )
    assert connection.execute(
        "SELECT count(*) FROM recommendation_delivery_outbox"
    ).fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 1


def test_scout_delivery_outbox_retries_until_acknowledged(library):
    connection, repo = library
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    outbox_id = repo.enqueue_scout_delivery(
        artifact_path="/profile/scout/scout-1.json",
        payload="weekly scout\n",
        created_at=now,
    )
    pending = repo.pending_scout_delivery()
    assert pending["id"] == outbox_id
    assert pending["payload_text"] == "weekly scout\n"

    repo.mark_scout_delivery_attempt(outbox_id, attempted_at=now)
    repo.reconcile_scout_delivery_attempt(
        completed_at=now, delivered=False, error="fresh context required",
    )
    assert repo.pending_scout_delivery()["last_error"] == "fresh context required"

    retry_at = now + timedelta(minutes=5)
    repo.mark_scout_delivery_attempt(outbox_id, attempted_at=retry_at)
    repo.reconcile_scout_delivery_attempt(completed_at=retry_at, delivered=True)
    assert repo.pending_scout_delivery() is None
    row = connection.execute(
        "SELECT status,attempt_count FROM scout_delivery_outbox WHERE id=?",
        (outbox_id,),
    ).fetchone()
    assert tuple(row) == ("delivered", 2)


def test_failed_recommendation_does_not_change_item_status(library):
    connection, repo = library
    item_id, now = _create_item(library)
    with pytest.raises(Exception):
        repo.record_recommendation(
            item_id, score=2.0, score_breakdown={}, recommended_at=now,
        )
    assert connection.execute("SELECT workflow_state FROM research_items WHERE id = ?", (item_id,)).fetchone()[0] == "discovered"
    assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 0


def test_feedback_and_learning_change_are_atomic(library):
    connection, repo = library
    item_id, now = _create_item(library)
    event_id = repo.record_feedback(
        item_id, kind="save", created_at=now, payload={"reason": "read later"},
    )
    assert event_id.startswith("fb_")
    row = connection.execute(
        "SELECT workflow_state,user_learning_status FROM research_items WHERE id = ?", (item_id,),
    ).fetchone()
    assert (row["workflow_state"], row["user_learning_status"]) == ("discovered", "saved")
    assert connection.execute("SELECT kind FROM feedback_events").fetchone()[0] == "save"

    repo.record_feedback(item_id, kind="note", created_at=now, payload={"text": "important"})
    assert connection.execute(
        "SELECT user_learning_status FROM research_items WHERE id = ?", (item_id,),
    ).fetchone()[0] == "saved"


def test_read_item_can_be_marked_synthesized(library):
    connection, repo = library
    item_id, now = _create_item(library)
    repo.record_feedback(item_id, kind="read", created_at=now)
    repo.record_feedback(item_id, kind="synthesize", created_at=now)

    row = connection.execute(
        "SELECT workflow_state,agent_analysis_status,user_learning_status "
        "FROM research_items WHERE id = ?", (item_id,)
    ).fetchone()
    assert tuple(row) == ("synthesized", "synthesized", "read")
    assert [row[0] for row in connection.execute(
        "SELECT kind FROM feedback_events WHERE item_id = ? ORDER BY rowid", (item_id,)
    )] == ["read", "synthesize"]


def test_feedback_for_unknown_item_does_not_write_event(library):
    connection, repo = library
    with pytest.raises(ValueError, match="Unknown Research Item"):
        repo.record_feedback(
            "missing", kind="save", created_at=datetime.now(timezone.utc),
        )
    assert connection.execute("SELECT count(*) FROM feedback_events").fetchone()[0] == 0


def test_learning_feedback_does_not_regress_agent_analysis(library):
    connection, repo = library
    item_id, now = _create_item(library)
    repo.set_analysis_status(item_id, status="deep_researched", updated_at=now)
    repo.record_feedback(item_id, kind="save", created_at=now)
    row = connection.execute(
        "SELECT agent_analysis_status,user_learning_status FROM research_items WHERE id=?",
        (item_id,),
    ).fetchone()
    assert tuple(row) == ("deep_researched", "saved")
    with pytest.raises(ValueError, match="deep_researched -> triaged"):
        repo.set_analysis_status(item_id, status="triaged", updated_at=now)


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
