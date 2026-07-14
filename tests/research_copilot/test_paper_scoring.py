"""Tests for research_copilot.paper_scoring.

Deterministic — no LLM, no I/O. Fixture inputs; verify each dim in
isolation plus rank_top_k composition + edge cases.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research_copilot import paper_scoring as ps


@pytest.fixture
def topics():
    return [
        {
            "id": "kv-cache",
            "name": "KV Cache and Attention",
            "priority": 0.9,
            "status": "active",
            "include": ["KV cache", "paged attention", "speculative decoding"],
        },
        {
            "id": "world-model",
            "name": "World Model Inference",
            "priority": 0.99,
            "status": "active",
            "include": ["video diffusion", "world model", "SSM"],
        },
        {
            "id": "dormant-topic",
            "name": "Dormant Topic",
            "priority": 0.4,
            "status": "dormant",
            "include": ["some old thing"],
        },
    ]


@pytest.fixture
def research_profile():
    return {
        "long_term_agenda": [
            {
                "id": "inf-opt",
                "name": "Inference Optimization",
                "priority": 0.99,
                "open_questions": [
                    "How to safely reuse KV cache across requests?",
                    "When to switch from KV-cache compression to linear-attention/SSM architectures?",
                ],
            },
            {
                "id": "serving",
                "name": "Serving Systems",
                "priority": 0.9,
                "open_questions": ["How to benchmark serving throughput?"],
            },
        ]
    }


@pytest.fixture
def config():
    return {
        "score_weights": {
            "relevance": 0.28,
            "open_question_match": 0.24,
            "novelty": 0.18,
            "source_tier": 0.12,
            "profile_role": 0.12,
            "actionability": 0.06,
        },
        "threshold": 0.65,
    }


def _cand(**over):
    base = {
        "id": "tk_a",
        "title": "A paper on some topic",
        "summary": "It discusses stuff.",
        "authors": ["Alice"],
        "url": "https://example/p",
        "arxiv_id": "9999.99999",
        "sources": [{"name": "arxiv_api_fallback", "topic": "KV Cache and Attention"}],
        "topics": [{"name": "KV Cache and Attention", "confidence": 0.5}],
        "status": "candidate",
    }
    base.update(over)
    return base


# ── resolve_source_tier ─────────────────────────────────────────────────

def test_source_tier_from_candidate_stamp():
    c = _cand(source_tier=0.85)
    assert ps.resolve_source_tier(c) == 0.85


def test_source_tier_from_default_map():
    c = _cand(sources=[{"name": "arxiv_api_fallback"}])
    assert ps.resolve_source_tier(c) == 0.5


def test_source_tier_picks_max_across_sources():
    c = _cand(sources=[{"name": "arxiv_api_fallback"}, {"name": "hf_daily"}])
    assert ps.resolve_source_tier(c) == 1.0


def test_source_tier_overrides_win():
    c = _cand(sources=[{"name": "custom_source"}])
    assert ps.resolve_source_tier(c, tier_overrides={"custom_source": 0.95}) == 0.95


# ── score_relevance ─────────────────────────────────────────────────────

def test_relevance_matches_topic_include(topics):
    c = _cand(
        title="Speculative decoding for LLMs",
        summary="We propose paged attention improvements.",
    )
    # topics[0].include = 3 items; matched: 'speculative decoding' + 'paged attention'
    score = ps.score_relevance(c, topics)
    assert 0.5 < score <= 1.0


def test_relevance_ignores_dormant_topic(topics):
    c = _cand(title="some old thing revisited", summary="")
    # "some old thing" only in dormant topic. Should not match.
    # But candidate might match confidence from candidate.topics — override that.
    c["topics"] = []
    assert ps.score_relevance(c, topics) == 0.0


def test_relevance_uses_candidate_confidence_when_higher(topics):
    c = _cand(title="Unrelated title", summary="Nothing here")
    c["topics"] = [{"name": "KV Cache and Attention", "confidence": 0.75}]
    # No include matches, but confidence stamp is 0.75
    assert ps.score_relevance(c, topics) == 0.75


# ── score_open_question_match ───────────────────────────────────────────

def test_open_question_match_zero(research_profile):
    c = _cand(title="Cat pictures", summary="Kittens")
    assert ps.score_open_question_match(c, research_profile) == 0.0


def test_open_question_match_one(research_profile):
    c = _cand(
        title="KV cache reuse across serving requests",
        summary="Discusses safe cross-request cache reuse.",
    )
    # matches 'KV cache' + 'reuse' + 'requests' from Q1
    score = ps.score_open_question_match(c, research_profile)
    assert 0 < score <= 1.0


def test_open_question_match_capped_at_one(research_profile):
    c = _cand(
        title="KV cache reuse, throughput benchmark, and linear-attention SSM architectures for serving",
        summary=(
            "How to benchmark serving throughput while switching between "
            "KV-cache compression and linear-attention architectures? "
            "Cross-request cache reuse safety in serving requests."
        ),
    )
    assert ps.score_open_question_match(c, research_profile) == 1.0


# ── score_novelty ───────────────────────────────────────────────────────

def test_novelty_never_recommended():
    c = _cand(id="new_id")
    assert ps.score_novelty(c, []) == 1.0


def test_novelty_recent_reject():
    c = _cand(id="recent_id")
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    recs = [{"id": "recent_id", "recommended_at": "2026-07-10T00:00:00+00:00"}]
    assert ps.score_novelty(c, recs, now=now, days=30) == 0.0


def test_novelty_beyond_window():
    c = _cand(id="old_id")
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    recs = [{"id": "old_id", "recommended_at": "2026-05-01T00:00:00+00:00"}]
    assert ps.score_novelty(c, recs, now=now, days=30) == 1.0


def test_novelty_missing_timestamp_treated_as_recent():
    c = _cand(id="stampless_id")
    recs = [{"id": "stampless_id"}]  # no recommended_at
    assert ps.score_novelty(c, recs) == 0.0


# ── score_profile_role ──────────────────────────────────────────────────

def test_profile_role_matches_topic_priority(topics):
    c = _cand()
    c["topics"] = [{"name": "World Model Inference"}]
    assert ps.score_profile_role(c, topics) == 0.99


def test_profile_role_no_match_returns_zero(topics):
    c = _cand()
    c["topics"] = [{"name": "Unknown Topic"}]
    assert ps.score_profile_role(c, topics) == 0.0


def test_profile_role_no_topics_returns_zero(topics):
    c = _cand()
    c["topics"] = []
    assert ps.score_profile_role(c, topics) == 0.0


# ── score_actionability ─────────────────────────────────────────────────

def test_actionability_full():
    c = _cand(
        arxiv_id="2607.05147",
        url="https://arxiv.org/abs/2607.05147",
        summary="Code at github.com/x/y",
    )
    assert ps.score_actionability(c) == 1.0


def test_actionability_minimal():
    c = {"title": "just a title"}
    assert ps.score_actionability(c) == 0.0


# ── score_candidate + rank_top_k ────────────────────────────────────────

def test_score_candidate_combines_weights(topics, research_profile, config):
    c = _cand(
        title="Speculative decoding for KV cache",
        summary="Paged attention with SSM.",
        arxiv_id="2607.99999",
        url="https://x",
    )
    c["topics"] = [{"name": "KV Cache and Attention", "confidence": 0.8}]
    result = ps.score_candidate(
        c,
        weights=config["score_weights"],
        topics=topics,
        research_profile=research_profile,
        recommendations=[],
    )
    # all dims present
    assert set(result["dims"]) == set(config["score_weights"])
    # score bounded
    assert 0.0 <= result["score"] <= 1.0
    # reasons string non-empty
    assert result["reasons"]


def test_rank_top_k_excludes_status_non_candidate(topics, research_profile, config):
    c_ok = _cand(id="a")
    c_ok["topics"] = [{"name": "KV Cache and Attention", "confidence": 0.7}]
    c_skip = _cand(id="b", status="recommended")
    c_skip["topics"] = [{"name": "KV Cache and Attention", "confidence": 0.7}]
    ranked = ps.rank_top_k(
        [c_ok, c_skip],
        weights=config["score_weights"],
        topics=topics,
        research_profile=research_profile,
        recommendations=[],
    )
    ids = [x["candidate"]["id"] for x in ranked]
    assert "a" in ids
    assert "b" not in ids


def test_rank_top_k_hard_excludes_recent_recommendations(
    topics, research_profile, config
):
    c1 = _cand(id="already_recommended")
    c1["topics"] = [{"name": "KV Cache and Attention", "confidence": 0.9}]
    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    recs = [
        {"id": "already_recommended", "recommended_at": "2026-07-10T00:00:00+00:00"}
    ]
    ranked = ps.rank_top_k(
        [c1],
        weights=config["score_weights"],
        topics=topics,
        research_profile=research_profile,
        recommendations=recs,
        now=now,
    )
    assert ranked == []


def test_rank_top_k_sorts_descending(topics, research_profile, config):
    # Give two candidates deterministically different scores.
    high = _cand(id="high")
    high["topics"] = [{"name": "World Model Inference", "confidence": 0.99}]
    high["title"] = "Video diffusion world model with SSM"
    low = _cand(id="low")
    low["topics"] = [{"name": "KV Cache and Attention", "confidence": 0.2}]
    low["title"] = "Unrelated stuff"
    low["summary"] = ""
    ranked = ps.rank_top_k(
        [low, high],
        weights=config["score_weights"],
        topics=topics,
        research_profile=research_profile,
        recommendations=[],
    )
    assert ranked[0]["candidate"]["id"] == "high"
    assert ranked[-1]["candidate"]["id"] == "low"


def test_rank_top_k_k_cap(topics, research_profile, config):
    cands = []
    for i in range(30):
        c = _cand(id=f"c{i}")
        c["topics"] = [{"name": "KV Cache and Attention", "confidence": 0.5}]
        cands.append(c)
    ranked = ps.rank_top_k(
        cands,
        weights=config["score_weights"],
        topics=topics,
        research_profile=research_profile,
        recommendations=[],
        k=5,
    )
    assert len(ranked) == 5


def test_rank_top_k_threshold_filter(topics, research_profile, config):
    # weak candidate — no topic keywords, no open question, minimal fields.
    c = _cand(
        id="weak",
        title="cats",
        summary="",
        arxiv_id="",
        url="",
        sources=[{"name": "unknown_source"}],
        topics=[],
    )
    ranked_lax = ps.rank_top_k(
        [c],
        weights=config["score_weights"],
        topics=topics,
        research_profile=research_profile,
        recommendations=[],
        include_below_threshold=True,
    )
    assert len(ranked_lax) == 1
    ranked_strict = ps.rank_top_k(
        [c],
        weights=config["score_weights"],
        topics=topics,
        research_profile=research_profile,
        recommendations=[],
        include_below_threshold=False,
    )
    assert len(ranked_strict) == 0


def test_rank_top_k_empty_input(topics, research_profile, config):
    assert ps.rank_top_k(
        [],
        weights=config["score_weights"],
        topics=topics,
        research_profile=research_profile,
        recommendations=[],
    ) == []
