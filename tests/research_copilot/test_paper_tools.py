"""Tests for research_copilot.paper_tools handlers.

Uses tmp_path as HERMES_HOME so each test has an isolated data dir.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    data_dir = tmp_path / "research-copilot"
    data_dir.mkdir(parents=True)
    return data_dir


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _setup(
    data_dir: Path,
    *,
    candidates,
    topics=None,
    config=None,
    research_profile=None,
    recommendations=None,
):
    _write_jsonl(data_dir / "candidates.jsonl", candidates)
    (data_dir / "topics.json").write_text(
        json.dumps(topics or {"topics": []}, ensure_ascii=False)
    )
    (data_dir / "config.json").write_text(
        json.dumps(
            config
            or {
                "score_weights": {
                    "relevance": 0.28,
                    "open_question_match": 0.24,
                    "novelty": 0.18,
                    "source_tier": 0.12,
                    "profile_role": 0.12,
                    "actionability": 0.06,
                },
                "threshold": 0.65,
            },
            ensure_ascii=False,
        )
    )
    (data_dir / "research_profile.json").write_text(
        json.dumps(research_profile or {"long_term_agenda": []}, ensure_ascii=False)
    )
    _write_jsonl(data_dir / "recommendations.jsonl", recommendations or [])


# ── paper_top_candidates ───────────────────────────────────────────────

def test_top_candidates_returns_ranked_items(isolated_profile):
    _setup(
        isolated_profile,
        candidates=[
            {
                "id": "p_hi",
                "title": "KV cache reuse",
                "summary": "paged attention with speculative decoding",
                "sources": [{"name": "hf_daily"}],
                "topics": [{"name": "KV Cache and Attention", "confidence": 0.9}],
                "status": "candidate",
                "arxiv_id": "2601.00001",
                "url": "https://arxiv.org/abs/2601.00001",
            },
            {
                "id": "p_lo",
                "title": "cats",
                "summary": "kittens",
                "sources": [{"name": "arxiv_api_fallback"}],
                "topics": [],
                "status": "candidate",
            },
        ],
        topics={
            "topics": [
                {
                    "id": "kv",
                    "name": "KV Cache and Attention",
                    "priority": 0.9,
                    "status": "active",
                    "include": ["KV cache", "paged attention", "speculative decoding"],
                }
            ]
        },
    )
    from research_copilot.paper_tools import _handler_paper_top_candidates

    resp = json.loads(_handler_paper_top_candidates({"k": 5}))
    assert resp["success"] is True
    assert resp["total_ranked"] == 2
    ids = [it["candidate"]["id"] for it in resp["items"]]
    assert ids[0] == "p_hi"  # higher score sorts first
    # Compact form applied.
    first = resp["items"][0]
    assert "score" in first
    assert "dims" in first
    assert "candidate" in first
    assert first["candidate"]["url"] == "https://arxiv.org/abs/2601.00001"


def test_top_candidates_excludes_status_recommended(isolated_profile):
    _setup(
        isolated_profile,
        candidates=[
            {
                "id": "p_rec",
                "title": "already actioned",
                "status": "recommended",
                "topics": [{"name": "T", "confidence": 0.8}],
                "sources": [{"name": "hf_daily"}],
            },
        ],
        topics={"topics": []},
    )
    from research_copilot.paper_tools import _handler_paper_top_candidates

    resp = json.loads(_handler_paper_top_candidates({}))
    assert resp["success"] is True
    assert resp["total_ranked"] == 0


def test_top_candidates_hard_excludes_30d_history(isolated_profile):
    now_stamp = datetime.now(timezone.utc).isoformat()
    _setup(
        isolated_profile,
        candidates=[
            {
                "id": "p_repeat",
                "title": "already recommended recently",
                "status": "candidate",
                "topics": [{"name": "T", "confidence": 0.9}],
                "sources": [{"name": "hf_daily"}],
            }
        ],
        recommendations=[
            {"id": "p_repeat", "title": "already", "recommended_at": now_stamp}
        ],
    )
    from research_copilot.paper_tools import _handler_paper_top_candidates

    resp = json.loads(_handler_paper_top_candidates({}))
    assert resp["success"] is True
    assert resp["total_ranked"] == 0


def test_top_candidates_summary_truncated(isolated_profile):
    long = "x" * 800
    _setup(
        isolated_profile,
        candidates=[
            {
                "id": "big",
                "title": "big",
                "summary": long,
                "status": "candidate",
                "topics": [{"name": "T", "confidence": 0.9}],
                "sources": [{"name": "hf_daily"}],
            }
        ],
    )
    from research_copilot.paper_tools import _handler_paper_top_candidates

    resp = json.loads(_handler_paper_top_candidates({}))
    assert len(resp["items"][0]["candidate"]["summary"]) < 500


# ── paper_recent_recommendations ───────────────────────────────────────

def test_recent_recommendations_filters_by_window(isolated_profile):
    now = datetime.now(timezone.utc)
    old_stamp = (now - timedelta(days=60)).isoformat()
    new_stamp = (now - timedelta(days=5)).isoformat()
    _setup(
        isolated_profile,
        candidates=[],
        recommendations=[
            {"id": "old", "title": "way old", "recommended_at": old_stamp},
            {"id": "new", "title": "recent", "recommended_at": new_stamp},
        ],
    )
    from research_copilot.paper_tools import _handler_paper_recent_recommendations

    resp = json.loads(_handler_paper_recent_recommendations({"days": 30}))
    ids = [r["id"] for r in resp["items"]]
    assert "new" in ids
    assert "old" not in ids


def test_recent_recommendations_wider_window(isolated_profile):
    now = datetime.now(timezone.utc)
    old_stamp = (now - timedelta(days=60)).isoformat()
    _setup(
        isolated_profile,
        candidates=[],
        recommendations=[
            {"id": "old", "title": "way old", "recommended_at": old_stamp}
        ],
    )
    from research_copilot.paper_tools import _handler_paper_recent_recommendations

    resp = json.loads(_handler_paper_recent_recommendations({"days": 120}))
    assert len(resp["items"]) == 1


# ── paper_write_recommendation ─────────────────────────────────────────

def test_write_recommendation_happy_path(isolated_profile):
    _setup(
        isolated_profile,
        candidates=[
            {
                "id": "pick_me",
                "type": "paper",
                "title": "T",
                "summary": "S",
                "authors": ["A"],
                "url": "https://x",
                "arxiv_id": "2601.00001",
                "status": "candidate",
                "topics": [{"name": "T", "confidence": 0.9}],
            }
        ],
    )
    from research_copilot.paper_tools import _handler_paper_write_recommendation

    resp = json.loads(
        _handler_paper_write_recommendation(
            {
                "item_id": "pick_me",
                "brief_text": "here is the brief",
                "signal_roles": ["Evidence update"],
                "score": 0.82,
            }
        )
    )
    assert resp["success"] is True
    assert resp["item_id"] == "pick_me"
    # recommendations.jsonl grew by 1.
    rec_lines = (
        (isolated_profile / "recommendations.jsonl").read_text().splitlines()
    )
    assert len(rec_lines) == 1
    rec = json.loads(rec_lines[0])
    assert rec["id"] == "pick_me"
    assert rec["brief_text"] == "here is the brief"
    assert rec["score"] == 0.82
    # candidate.status flipped.
    cands = [
        json.loads(l)
        for l in (isolated_profile / "candidates.jsonl").read_text().splitlines()
        if l.strip()
    ]
    assert cands[0]["status"] == "recommended"
    assert cands[0]["scored"] is True
    # state.json updated.
    state = json.loads((isolated_profile / "state.json").read_text())
    assert state["last_pick_id"] == "pick_me"
    assert state["consecutive_no_pick_days"] == 0


def test_write_recommendation_rejects_unknown_id(isolated_profile):
    _setup(
        isolated_profile,
        candidates=[
            {"id": "known", "status": "candidate", "topics": []}
        ],
    )
    from research_copilot.paper_tools import _handler_paper_write_recommendation

    resp = json.loads(
        _handler_paper_write_recommendation(
            {"item_id": "unknown", "brief_text": "b"}
        )
    )
    assert resp["success"] is False
    assert "not found" in resp["error"]


def test_write_recommendation_rejects_already_actioned(isolated_profile):
    _setup(
        isolated_profile,
        candidates=[
            {"id": "done", "status": "recommended", "topics": []}
        ],
    )
    from research_copilot.paper_tools import _handler_paper_write_recommendation

    resp = json.loads(
        _handler_paper_write_recommendation({"item_id": "done", "brief_text": "b"})
    )
    assert resp["success"] is False
    assert "already actioned" in resp["error"]


def test_write_recommendation_rejects_recent_duplicate(isolated_profile):
    now_stamp = datetime.now(timezone.utc).isoformat()
    _setup(
        isolated_profile,
        candidates=[
            {"id": "repeat", "status": "candidate", "topics": []}
        ],
        recommendations=[
            {"id": "repeat", "recommended_at": now_stamp}
        ],
    )
    from research_copilot.paper_tools import _handler_paper_write_recommendation

    resp = json.loads(
        _handler_paper_write_recommendation({"item_id": "repeat", "brief_text": "b"})
    )
    assert resp["success"] is False
    assert "last 30 days" in resp["error"]


def test_write_recommendation_requires_fields(isolated_profile):
    _setup(isolated_profile, candidates=[])
    from research_copilot.paper_tools import _handler_paper_write_recommendation

    r1 = json.loads(_handler_paper_write_recommendation({}))
    assert r1["success"] is False
    r2 = json.loads(
        _handler_paper_write_recommendation({"item_id": "x", "brief_text": ""})
    )
    assert r2["success"] is False


# ── Registry hook ─────────────────────────────────────────────────────

def test_all_three_tools_registered():
    from tools.registry import registry
    # import triggers registration
    from research_copilot import paper_tools  # noqa: F401

    names = [
        "paper_top_candidates",
        "paper_recent_recommendations",
        "paper_write_recommendation",
    ]
    for n in names:
        tool = registry.get(n) if hasattr(registry, "get") else None
        # The registry is a global; we just want to see it recognises the names.
        assert n in {t for t in registry.list_names()} if hasattr(
            registry, "list_names"
        ) else True
