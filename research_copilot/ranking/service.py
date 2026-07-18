"""Application service joining Library persistence and pure scoring."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from research_copilot.library import LibraryRepository

from .scoring import DEFAULT_WEIGHTS, RankingItem, ScoreResult, TopicPolicy, rank_items


class RankingService:
    def __init__(self, repository: LibraryRepository):
        self.repository = repository

    def rank(
        self,
        *,
        topics: Mapping[str, TopicPolicy],
        threshold: float = 0.0,
        limit: int = 20,
        source_saturation: Mapping[str, float] | None = None,
        weights: Mapping[str, float] = DEFAULT_WEIGHTS,
        now: datetime | None = None,
    ) -> list[ScoreResult]:
        items = [RankingItem(**record) for record in self.repository.list_ranking_records()]
        return rank_items(
            items, topics=topics, threshold=threshold, limit=limit,
            source_saturation=source_saturation, weights=weights, now=now,
        )

    def recommend_top(
        self,
        *,
        topics: Mapping[str, TopicPolicy],
        recommended_at: datetime,
        threshold: float,
        source_saturation: Mapping[str, float] | None = None,
        weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    ) -> ScoreResult | None:
        results = self.rank(
            topics=topics, threshold=threshold, limit=1,
            source_saturation=source_saturation, weights=weights,
            now=recommended_at,
        )
        if not results:
            return None
        winner = results[0]
        self.repository.record_recommendation(
            winner.item_id,
            score=winner.score,
            score_breakdown=dict(winner.dimensions),
            recommended_at=recommended_at,
            rationale=" · ".join(winner.reasons),
        )
        return winner
