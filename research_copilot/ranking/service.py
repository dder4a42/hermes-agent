"""Application service joining Library persistence and pure scoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Mapping

from research_copilot.library import LibraryRepository

from .scoring import (
    AgendaPolicy, DEFAULT_WEIGHTS, RankingItem, ScoreResult, TopicPolicy, rank_items,
)


@dataclass(frozen=True)
class RecommendationBudget:
    daily_limit: int = 1
    weekly_limit: int = 3


@dataclass(frozen=True)
class RecommendationBudgetStatus:
    allowed: bool
    daily_used: int
    daily_limit: int
    weekly_used: int
    weekly_limit: int


@dataclass(frozen=True)
class TriageCard:
    item_id: str
    title: str
    excerpt: str
    ranking: ScoreResult


class RankingService:
    def __init__(self, repository: LibraryRepository):
        self.repository = repository

    def rank(
        self,
        *,
        topics: Mapping[str, TopicPolicy],
        agendas: Mapping[str, AgendaPolicy] | None = None,
        threshold: float = 0.0,
        limit: int = 20,
        source_saturation: Mapping[str, float] | None = None,
        weights: Mapping[str, float] = DEFAULT_WEIGHTS,
        now: datetime | None = None,
        secondary_topic_bonus_cap: float = 0.10,
    ) -> list[ScoreResult]:
        items = [RankingItem(**record) for record in self.repository.list_ranking_records()]
        return rank_items(
            items, topics=topics, agendas=agendas, threshold=threshold, limit=limit,
            source_saturation=source_saturation, weights=weights, now=now,
            secondary_topic_bonus_cap=secondary_topic_bonus_cap,
        )

    def triage(
        self, *, topics: Mapping[str, TopicPolicy], agendas: Mapping[str, AgendaPolicy] | None = None,
        threshold: float = 0.0, limit: int = 10, max_chars: int = 200,
        secondary_topic_bonus_cap: float = 0.10, now: datetime | None = None,
    ) -> list[TriageCard]:
        records = self.repository.list_ranking_records()
        by_id = {str(record["id"]): record for record in records}
        ranked = rank_items(
            [RankingItem(**record) for record in records], topics=topics, agendas=agendas,
            threshold=threshold, limit=limit, now=now,
            secondary_topic_bonus_cap=secondary_topic_bonus_cap,
        )
        cards: list[TriageCard] = []
        for result in ranked:
            record = by_id[result.item_id]
            excerpt = re.sub(r"\s+", " ", str(record.get("summary") or "")).strip()
            if len(excerpt) > max_chars:
                excerpt = excerpt[: max(1, max_chars - 1)].rstrip() + "…"
            cards.append(TriageCard(
                item_id=result.item_id, title=str(record.get("title") or "Untitled"),
                excerpt=excerpt, ranking=result,
            ))
        return cards

    def budget_status(
        self, *, at: datetime, budget: RecommendationBudget,
    ) -> RecommendationBudgetStatus:
        day_start, week_start = self._budget_windows(at)
        daily_used = self.repository.recommendation_count_since(day_start)
        weekly_used = self.repository.recommendation_count_since(week_start)
        return RecommendationBudgetStatus(
            allowed=daily_used < budget.daily_limit and weekly_used < budget.weekly_limit,
            daily_used=daily_used, daily_limit=budget.daily_limit,
            weekly_used=weekly_used, weekly_limit=budget.weekly_limit,
        )

    @staticmethod
    def _budget_windows(at: datetime) -> tuple[datetime, datetime]:
        at = at.astimezone(timezone.utc)
        day_start = at.replace(hour=0, minute=0, second=0, microsecond=0)
        return day_start, day_start - timedelta(days=day_start.weekday())

    def recommend_top(
        self,
        *,
        topics: Mapping[str, TopicPolicy],
        agendas: Mapping[str, AgendaPolicy] | None = None,
        recommended_at: datetime,
        threshold: float,
        source_saturation: Mapping[str, float] | None = None,
        weights: Mapping[str, float] = DEFAULT_WEIGHTS,
        budget: RecommendationBudget | None = None,
        secondary_topic_bonus_cap: float = 0.10,
        enqueue_delivery: bool = False,
    ) -> ScoreResult | None:
        if budget is not None and not self.budget_status(at=recommended_at, budget=budget).allowed:
            return None
        results = self.rank(
            topics=topics, agendas=agendas, threshold=threshold, limit=1,
            source_saturation=source_saturation, weights=weights,
            now=recommended_at, secondary_topic_bonus_cap=secondary_topic_bonus_cap,
        )
        if not results:
            return None
        winner = results[0]
        recommendation_args = {
            "score": winner.score,
            "score_breakdown": dict(winner.dimensions),
            "recommended_at": recommended_at,
            "rationale": " · ".join(winner.reasons),
        }
        if budget is None:
            self.repository.record_recommendation(
                winner.item_id, **recommendation_args,
                enqueue_delivery=enqueue_delivery,
            )
        else:
            day_start, week_start = self._budget_windows(recommended_at)
            recommendation_id = self.repository.record_recommendation_with_budget(
                winner.item_id, **recommendation_args,
                daily_since=day_start, daily_limit=budget.daily_limit,
                weekly_since=week_start, weekly_limit=budget.weekly_limit,
                enqueue_delivery=enqueue_delivery,
            )
            if recommendation_id is None:
                return None
        return winner
