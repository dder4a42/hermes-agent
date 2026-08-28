from __future__ import annotations

from datetime import datetime, timezone

from research_copilot.library import (
    LibraryRepository, ResearchItemDraft, SourceEvidence, TopicMatch,
    connect_library, initialize_library,
)
from research_copilot.ranking import RankingService, TopicPolicy


def _setup(tmp_path):
    connection = connect_library(tmp_path / "library.db")
    initialize_library(connection, migrated_at="2026-07-17T00:00:00+00:00")
    return connection, LibraryRepository(connection)


def test_ranking_projects_library_relations_and_recommends_atomically(tmp_path):
    connection, repository = _setup(tmp_path)
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    try:
        for source_id, tier in (("hf", 1.0), ("s2", 0.85)):
            repository.upsert_source(
                source_id=source_id, provider=source_id, display_name=source_id,
                source_type="index", tier=tier, now=now,
            )
        draft = ResearchItemDraft(
            title="Research agent evidence chains", summary="Evidence chains across searches",
            url="https://arxiv.org/abs/2607.00001", arxiv_id="2607.00001",
            published_at="2026-07-16T00:00:00+00:00",
        )
        first = repository.upsert_item(
            draft, source=SourceEvidence("hf"),
            topics=(TopicMatch("agent", 0.9),), discovered_at=now,
        )
        repository.upsert_item(
            draft, source=SourceEvidence("s2"),
            topics=(TopicMatch("agent", 0.9),), discovered_at=now,
        )
        winner = RankingService(repository).recommend_top(
            topics={"agent": TopicPolicy("agent", 0.95, ("research agent",))},
            recommended_at=now, threshold=0.5,
        )
        assert winner is not None
        assert winner.item_id == first.item_id
        assert winner.dimensions["multi_source_confirmation"] == 0.5
        item = connection.execute("SELECT status FROM research_items").fetchone()
        recommendation = connection.execute("SELECT * FROM recommendations").fetchone()
        assert item["status"] == "recommended"
        assert recommendation["item_id"] == first.item_id
        assert recommendation["score"] == winner.score
    finally:
        connection.close()


def test_recommend_top_is_silent_below_threshold(tmp_path):
    connection, repository = _setup(tmp_path)
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    try:
        repository.upsert_source(
            source_id="low", provider="low", display_name="low",
            source_type="raw", tier=0.1, now=now,
        )
        repository.upsert_item(
            ResearchItemDraft(title="Unrelated"), source=SourceEvidence("low"),
            discovered_at=now,
        )
        result = RankingService(repository).recommend_top(
            topics={}, recommended_at=now, threshold=0.9,
        )
        assert result is None
        assert connection.execute("SELECT count(*) FROM recommendations").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM research_items").fetchone()[0] == "discovered"
    finally:
        connection.close()
