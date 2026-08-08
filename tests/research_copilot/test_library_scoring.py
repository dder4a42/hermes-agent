from __future__ import annotations

from datetime import datetime, timezone

from research_copilot.ranking import (
    AgendaPolicy, PromptPolicy, RankingItem, TopicPolicy, rank_items, score_item,
)


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


def test_profile_matches_keep_auditable_ids_and_suggest_deep_read():
    agendas = {
        "reliable-agents": AgendaPolicy(
            id="reliable-agents", priority=0.95, topic_ids=("agent",),
            open_questions=(PromptPolicy("oq-recovery", "How should evidence chains be maintained?"),),
            knowledge_gaps=(PromptPolicy("gap-traces", "Need evidence chain failure traces"),),
        ),
    }
    result = score_item(
        _item(summary="Maintaining evidence chains with real failure traces"),
        topics=TOPICS, agendas=agendas, now=NOW,
    )
    assert result.primary_topic_id == "agent"
    assert result.matched_agenda_ids == ("reliable-agents",)
    assert result.matched_question_ids == ("oq-recovery",)
    assert result.matched_knowledge_gap_ids == ("gap-traces",)
    assert result.suggested_action == "deep-read"


def test_secondary_topics_add_only_a_bounded_bonus_and_do_not_take_primary_priority():
    topics = {
        "primary": TopicPolicy("primary", 0.4),
        "secondary-a": TopicPolicy("secondary-a", 1.0),
        "secondary-b": TopicPolicy("secondary-b", 1.0),
    }
    item = _item(topic_confidences={"primary": 0.9, "secondary-a": 0.8, "secondary-b": 0.7})
    result = score_item(item, topics=topics, now=NOW, secondary_topic_bonus_cap=0.05)
    assert result.primary_topic_id == "primary"
    assert result.secondary_topic_ids == ("secondary-a", "secondary-b")
    assert result.dimensions["topic_relevance"] == 0.95
    assert result.dimensions["topic_priority"] == 0.4


def test_profile_matching_supports_chinese_questions():
    agendas = {
        "long-horizon": AgendaPolicy(
            id="long-horizon", priority=0.9, topic_ids=("agent",),
            open_questions=(PromptPolicy("oq-credit", "长程任务如何进行信用分配"),),
        ),
    }
    result = score_item(
        _item(summary="研究长程任务中的信用分配与恢复机制"),
        topics=TOPICS, agendas=agendas, now=NOW,
    )
    assert result.matched_question_ids == ("oq-credit",)
