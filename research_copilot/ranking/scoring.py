"""Deterministic and auditable Research Library scoring."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

DEFAULT_WEIGHTS: dict[str, float] = {
    "topic_relevance": 0.28,
    "topic_priority": 0.16,
    "open_question_match": 0.14,
    "source_quality": 0.14,
    "multi_source_confirmation": 0.10,
    "freshness": 0.10,
    "actionability": 0.08,
    "source_saturation_penalty": 0.15,
}


@dataclass(frozen=True)
class TopicPolicy:
    id: str
    priority: float
    include: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class RankingItem:
    id: str
    title: str
    summary: str = ""
    url: str = ""
    item_type: str = "research_signal"
    published_at: str | None = None
    identifiers: Mapping[str, str] = field(default_factory=dict)
    source_tiers: Mapping[str, float] = field(default_factory=dict)
    topic_confidences: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreResult:
    item_id: str
    score: float
    dimensions: Mapping[str, float]
    reasons: tuple[str, ...]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _text(item: RankingItem) -> str:
    return f"{item.title} {item.summary} {item.url}".casefold()


def _question_match(text: str, questions: tuple[str, ...]) -> float:
    matched = 0
    for question in questions:
        tokens = [token for token in re.findall(r"[\w]+", question.casefold()) if len(token) > 3][:8]
        if tokens and sum(token in text for token in tokens) >= min(2, len(tokens)):
            matched += 1
    return min(1.0, matched / 3.0)


def _freshness(published_at: str | None, now: datetime) -> float:
    if not published_at:
        return 0.3
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0.3
    age_days = max(0, (now.astimezone(timezone.utc) - published.astimezone(timezone.utc)).days)
    if age_days <= 7:
        return 1.0
    if age_days <= 30:
        return 0.8
    if age_days <= 90:
        return 0.5
    return 0.2


def score_item(
    item: RankingItem,
    *,
    topics: Mapping[str, TopicPolicy],
    source_saturation: Mapping[str, float] | None = None,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    now: datetime | None = None,
) -> ScoreResult:
    now = now or datetime.now(timezone.utc)
    text = _text(item)
    topic_scores: list[tuple[TopicPolicy, float]] = []
    for topic_id, confidence in item.topic_confidences.items():
        topic = topics.get(topic_id)
        if topic is None:
            continue
        terms = tuple(term for term in topic.include if term)
        overlap = sum(term.casefold() in text for term in terms) / len(terms) if terms else 0.0
        topic_scores.append((topic, max(_clamp(confidence), overlap)))
    best_topic, relevance = max(topic_scores, key=lambda pair: pair[1], default=(None, 0.0))
    priority = _clamp(best_topic.priority) if best_topic else 0.0
    questions = best_topic.open_questions if best_topic else ()
    source_quality = max((_clamp(tier) for tier in item.source_tiers.values()), default=0.0)
    source_count = len(item.source_tiers)
    multi_source = min(1.0, max(0, source_count - 1) / 2.0)
    actionable = 0.0
    if "doi" in item.identifiers or "arxiv" in item.identifiers:
        actionable += 0.6
    if item.url:
        actionable += 0.2
    if item.item_type == "project" or "github.com" in item.url.casefold():
        actionable += 0.2
    saturation = max(
        ((_clamp((source_saturation or {}).get(source_id, 0.0))) for source_id in item.source_tiers),
        default=0.0,
    )
    dimensions = {
        "topic_relevance": _clamp(relevance),
        "topic_priority": priority,
        "open_question_match": _question_match(text, questions),
        "source_quality": source_quality,
        "multi_source_confirmation": multi_source,
        "freshness": _freshness(item.published_at, now),
        "actionability": _clamp(actionable),
        "source_saturation_penalty": saturation,
    }
    score = sum(
        dimensions[name] * float(weight)
        for name, weight in weights.items()
        if name != "source_saturation_penalty" and name in dimensions
    )
    score -= dimensions["source_saturation_penalty"] * float(weights.get("source_saturation_penalty", 0.0))
    reasons = tuple(
        f"{name}={value:.2f}"
        for name, value in dimensions.items()
        if value > 0
    )
    return ScoreResult(
        item_id=item.id,
        score=round(_clamp(score), 4),
        dimensions=dimensions,
        reasons=reasons,
    )


def rank_items(
    items: list[RankingItem],
    *,
    topics: Mapping[str, TopicPolicy],
    threshold: float = 0.0,
    limit: int = 20,
    source_saturation: Mapping[str, float] | None = None,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    now: datetime | None = None,
) -> list[ScoreResult]:
    results = [
        score_item(
            item, topics=topics, source_saturation=source_saturation,
            weights=weights, now=now,
        )
        for item in items
    ]
    results = [result for result in results if result.score >= threshold]
    results.sort(key=lambda result: (result.score, result.item_id), reverse=True)
    return results[: max(0, limit)]
