from __future__ import annotations

from datetime import datetime, timezone

from research_copilot.ranking import RankingItem, TopicPolicy, rank_items, score_item


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)
TOPICS = {
    "agent": TopicPolicy(
        id="agent", priority=0.9,
        include=("research agent", "evidence chain"),
        open_questions=("How should evidence chains be maintained across searches?",),
    )
}


def _item(**changes):
    values = dict(
        id="one", title="Research agent evidence chain",
        summary="Maintaining evidence chains across long searches",
        url="https://arxiv.org/abs/2607.00001", item_type="paper",
        published_at="2026-07-16T00:00:00+00:00",
        identifiers={"arxiv": "2607.00001"},
        source_tiers={"hf": 1.0}, topic_confidences={"agent": 0.8},
    )
    values.update(changes)
    return RankingItem(**values)


def test_score_has_explainable_bounded_dimensions():
    result = score_item(_item(), topics=TOPICS, now=NOW)
    assert 0 <= result.score <= 1
    assert result.dimensions["topic_relevance"] == 1.0
    assert result.dimensions["source_quality"] == 1.0
    assert result.dimensions["freshness"] == 1.0
    assert result.dimensions["actionability"] == 0.8
    assert "topic_relevance=1.00" in result.reasons


def test_independent_sources_raise_confirmation_dimension():
    single = score_item(_item(), topics=TOPICS, now=NOW)
    multiple = score_item(
        _item(source_tiers={"hf": 1.0, "s2": 0.85, "rss": 0.8}),
        topics=TOPICS, now=NOW,
    )
    assert single.dimensions["multi_source_confirmation"] == 0.0
    assert multiple.dimensions["multi_source_confirmation"] == 1.0
    assert multiple.score > single.score


def test_source_saturation_penalty_lowers_score():
    normal = score_item(_item(), topics=TOPICS, now=NOW)
    saturated = score_item(
        _item(), topics=TOPICS, source_saturation={"hf": 1.0}, now=NOW,
    )
    assert saturated.dimensions["source_saturation_penalty"] == 1.0
    assert saturated.score < normal.score


def test_unknown_dates_are_neutral_not_fresh():
    result = score_item(_item(published_at="not-a-date"), topics=TOPICS, now=NOW)
    assert result.dimensions["freshness"] == 0.3


def test_rank_is_deterministic_and_applies_threshold_and_limit():
    strong = _item(id="strong")
    weak = _item(
        id="weak", title="Unrelated", summary="", url="",
        published_at=None, identifiers={}, source_tiers={"arxiv": 0.5},
        topic_confidences={"agent": 0.1},
    )
    ranked = rank_items([weak, strong], topics=TOPICS, threshold=0.5, limit=1, now=NOW)
    assert [result.item_id for result in ranked] == ["strong"]
