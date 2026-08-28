"""Deterministic and auditable Research Library scoring."""

from __future__ import annotations

import re
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

DEFAULT_WEIGHTS: dict[str, float] = {
    "topic_relevance": 0.22,
    "topic_priority": 0.12,
    "open_question_match": 0.12,
    "knowledge_gap_match": 0.10,
    "source_quality": 0.14,
    "multi_source_confirmation": 0.10,
    "freshness": 0.10,
    "actionability": 0.10,
    "source_saturation_penalty": 0.15,
}


@dataclass(frozen=True)
class TopicPolicy:
    id: str
    priority: float
    include: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PromptPolicy:
    id: str
    text: str


@dataclass(frozen=True)
class AgendaPolicy:
    id: str
    priority: float
    topic_ids: tuple[str, ...]
    open_questions: tuple[PromptPolicy, ...] = ()
    knowledge_gaps: tuple[PromptPolicy, ...] = ()


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
    primary_topic_id: str | None = None
    secondary_topic_ids: tuple[str, ...] = ()
    matched_agenda_ids: tuple[str, ...] = ()
    matched_question_ids: tuple[str, ...] = ()
    matched_knowledge_gap_ids: tuple[str, ...] = ()
    suggested_action: str = "review"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _text(item: RankingItem) -> str:
    return f"{item.title} {item.summary} {item.url}".casefold()


def _prompt_matches(text: str, prompts: tuple[PromptPolicy, ...]) -> tuple[float, tuple[str, ...]]:
    stopwords = {
        "between", "does", "from", "have", "into", "need", "remain", "should",
        "that", "their", "these", "used", "what", "when", "which", "with", "would",
    }
    matched: list[str] = []
    for prompt in prompts:
        folded = prompt.text.casefold()
        tokens = [
            token for token in re.findall(r"[a-z0-9_]+", folded)
            if len(token) > 3 and token not in stopwords
        ][:8]
        cjk_chunks = re.findall(r"[\u3400-\u9fff]+", folded)
        cjk_bigrams = tuple(dict.fromkeys(
            chunk[index:index + 2]
            for chunk in cjk_chunks for index in range(max(0, len(chunk) - 1))
        ))[:12]
        latin_required = max(2, math.ceil(len(tokens) * 0.6))
        cjk_required = max(2, math.ceil(len(cjk_bigrams) * 0.35))
        latin_match = bool(tokens) and sum(token in text for token in tokens) >= latin_required
        cjk_match = bool(cjk_bigrams) and sum(term in text for term in cjk_bigrams) >= cjk_required
        if latin_match or cjk_match:
            if prompt.id not in matched:
                matched.append(prompt.id)
    return min(1.0, len(matched) / 3.0), tuple(matched)


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
    agendas: Mapping[str, AgendaPolicy] | None = None,
    source_saturation: Mapping[str, float] | None = None,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    now: datetime | None = None,
    secondary_topic_bonus_cap: float = 0.10,
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
    topic_scores = [pair for pair in topic_scores if pair[1] > 0]
    topic_scores.sort(key=lambda pair: (-pair[1], pair[0].id))
    best_topic, primary_relevance = topic_scores[0] if topic_scores else (None, 0.0)
    secondary_topics = tuple(topic.id for topic, _score in topic_scores[1:])
    secondary_bonus = min(
        max(0.0, float(secondary_topic_bonus_cap)),
        sum(score for _topic, score in topic_scores[1:]) * 0.05,
    )
    relevance = _clamp(primary_relevance + secondary_bonus)
    linked_agendas = tuple(
        agenda for agenda in (agendas or {}).values()
        if any(topic.id in agenda.topic_ids for topic, _score in topic_scores)
    )
    primary_agendas = tuple(
        agenda for agenda in linked_agendas
        if best_topic is not None and best_topic.id in agenda.topic_ids
    )
    priority = max((_clamp(agenda.priority) for agenda in primary_agendas), default=None)
    if priority is None:
        priority = _clamp(best_topic.priority) if best_topic else 0.0
    question_prompts = tuple(prompt for agenda in linked_agendas for prompt in agenda.open_questions)
    gap_prompts = tuple(prompt for agenda in linked_agendas for prompt in agenda.knowledge_gaps)
    if not question_prompts and best_topic:
        question_prompts = tuple(
            PromptPolicy(f"{best_topic.id}:open-question-{index + 1}", value)
            for index, value in enumerate(best_topic.open_questions)
        )
    question_match, question_ids = _prompt_matches(text, question_prompts)
    gap_match, gap_ids = _prompt_matches(text, gap_prompts)
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
        "topic_relevance": round(_clamp(relevance), 4),
        "topic_priority": priority,
        "open_question_match": question_match,
        "knowledge_gap_match": gap_match,
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
    bounded_score = round(_clamp(score), 4)
    dimension_reasons = tuple(
        f"{name}={value:.2f}"
        for name, value in dimensions.items()
        if value > 0
    )
    matched_agenda_ids = tuple(sorted({agenda.id for agenda in linked_agendas}))
    if bounded_score >= 0.70 and (question_ids or gap_ids):
        suggested_action = "deep-read"
    elif bounded_score >= 0.50:
        suggested_action = "shortlist"
    else:
        suggested_action = "triage"
    identity_reasons = tuple(value for value in (
        f"primary_topic={best_topic.id}" if best_topic else "",
        f"secondary_topics={','.join(secondary_topics)}" if secondary_topics else "",
        f"agenda={','.join(matched_agenda_ids)}" if matched_agenda_ids else "",
        f"questions={','.join(question_ids)}" if question_ids else "",
        f"knowledge_gaps={','.join(gap_ids)}" if gap_ids else "",
        f"suggested_action={suggested_action}",
    ) if value)
    return ScoreResult(
        item_id=item.id,
        score=bounded_score,
        dimensions=dimensions,
        reasons=identity_reasons + dimension_reasons,
        primary_topic_id=best_topic.id if best_topic else None,
        secondary_topic_ids=secondary_topics,
        matched_agenda_ids=matched_agenda_ids,
        matched_question_ids=question_ids,
        matched_knowledge_gap_ids=gap_ids,
        suggested_action=suggested_action,
    )


def rank_items(
    items: list[RankingItem],
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
    results = [
        score_item(
            item, topics=topics, agendas=agendas, source_saturation=source_saturation,
            weights=weights, now=now, secondary_topic_bonus_cap=secondary_topic_bonus_cap,
        )
        for item in items
    ]
    results = [result for result in results if result.score >= threshold]
    results.sort(key=lambda result: (result.score, result.item_id), reverse=True)
    return results[: max(0, limit)]
