"""Tests for research_copilot.scripts.paper_health.build_report."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from research_copilot.scripts.paper_health import build_report


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "research-copilot"
    d.mkdir()
    return d


def _write_json(path: Path, obj):
    path.write_text(json.dumps(obj))


def _write_jsonl(path: Path, rows: list[dict]):
    path.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""))


def test_empty_profile_reports_zero_counts(data_dir):
    _write_json(data_dir / "state.json", {})
    _write_json(data_dir / "topics.json", {"topics": []})
    report = build_report(data_dir=data_dir, window_days=7)
    assert "Recommendations delivered: 0" in report
    assert "0 save · 0 read · 0 skip · 0 note" in report
    # No proposals when there's no data.
    assert "Suggested adjustments" not in report


def test_counts_interactions_within_window(data_dir):
    now = datetime.now(timezone.utc)
    # One save in-window, one save out-of-window (older than 7 days).
    _write_jsonl(
        data_dir / "interactions.jsonl",
        [
            {"kind": "save", "item_id": "a", "at": now.isoformat()},
            {"kind": "save", "item_id": "b", "at": (now - timedelta(days=30)).isoformat()},
            {"kind": "skip", "item_id": "c", "at": now.isoformat()},
        ],
    )
    _write_jsonl(data_dir / "recommendations.jsonl", [])
    _write_jsonl(data_dir / "candidates.jsonl", [])
    _write_json(data_dir / "state.json", {})
    _write_json(data_dir / "topics.json", {"topics": []})

    report = build_report(data_dir=data_dir, window_days=7, now=now)
    assert "1 save · 0 read · 1 skip · 0 note" in report


def test_flags_no_pick_streak_at_three_days(data_dir):
    _write_json(data_dir / "state.json", {"consecutive_no_pick_days": 4})
    _write_json(data_dir / "topics.json", {"topics": []})
    report = build_report(data_dir=data_dir, window_days=7)
    assert "WARNING: no pick for 4 days" in report


def test_topic_engagement_lines_present(data_dir):
    now = datetime.now(timezone.utc)
    _write_jsonl(
        data_dir / "recommendations.jsonl",
        [
            {"id": "p1", "recommended_at": now.isoformat(), "score": 0.9,
             "title": "T1", "topic_matches": ["Research Agent"]},
            {"id": "p2", "recommended_at": now.isoformat(), "score": 0.8,
             "title": "T2", "topic_matches": ["Multimodal LMM"]},
        ],
    )
    _write_jsonl(
        data_dir / "interactions.jsonl",
        [
            {"kind": "save", "item_id": "p1", "at": now.isoformat()},
            {"kind": "skip", "item_id": "p2", "at": now.isoformat()},
        ],
    )
    _write_jsonl(data_dir / "candidates.jsonl", [])
    _write_json(data_dir / "state.json", {})
    _write_json(data_dir / "topics.json", {"topics": []})

    report = build_report(data_dir=data_dir, window_days=7, now=now)
    assert "Topic engagement:" in report
    assert "Research Agent: 1 save/read" in report
    assert "Multimodal LMM: 0 save/read, 1 skip" in report


def test_proposal_lowers_topic_with_three_skips_and_zero_saves(data_dir):
    now = datetime.now(timezone.utc)
    ix = []
    recs = []
    for i in range(3):
        pid = f"p{i}"
        recs.append({
            "id": pid, "recommended_at": now.isoformat(), "score": 0.7,
            "title": f"T{i}", "topic_matches": ["Junky Topic"],
        })
        ix.append({"kind": "skip", "item_id": pid, "at": now.isoformat()})
    _write_jsonl(data_dir / "recommendations.jsonl", recs)
    _write_jsonl(data_dir / "interactions.jsonl", ix)
    _write_jsonl(data_dir / "candidates.jsonl", [])
    _write_json(data_dir / "state.json", {})
    _write_json(data_dir / "topics.json", {"topics": []})

    report = build_report(data_dir=data_dir, window_days=7, now=now)
    assert "Suggested adjustments" in report
    assert "lower priority for topic \"Junky Topic\"" in report


def test_proposal_raises_source_with_multiple_saves(data_dir):
    now = datetime.now(timezone.utc)
    _write_jsonl(
        data_dir / "candidates.jsonl",
        [
            {"id": "p1", "type": "paper", "title": "T1", "url": "u",
             "discovered_at": now.isoformat(), "status": "saved",
             "sources": [{"name": "GreatFeed"}]},
            {"id": "p2", "type": "paper", "title": "T2", "url": "u",
             "discovered_at": now.isoformat(), "status": "saved",
             "sources": [{"name": "GreatFeed"}]},
        ],
    )
    _write_jsonl(
        data_dir / "interactions.jsonl",
        [
            {"kind": "save", "item_id": "p1", "at": now.isoformat()},
            {"kind": "save", "item_id": "p2", "at": now.isoformat()},
        ],
    )
    _write_jsonl(data_dir / "recommendations.jsonl", [])
    _write_json(data_dir / "state.json", {})
    _write_json(data_dir / "topics.json", {"topics": []})

    report = build_report(data_dir=data_dir, window_days=7, now=now)
    assert "raise tier for source \"GreatFeed\"" in report


def test_all_time_window_zero_disables_filter(data_dir):
    old = datetime(2024, 1, 1, tzinfo=timezone.utc)
    _write_jsonl(
        data_dir / "interactions.jsonl",
        [{"kind": "save", "item_id": "old", "at": old.isoformat()}],
    )
    _write_jsonl(data_dir / "recommendations.jsonl", [])
    _write_jsonl(data_dir / "candidates.jsonl", [])
    _write_json(data_dir / "state.json", {})
    _write_json(data_dir / "topics.json", {"topics": []})

    windowed = build_report(data_dir=data_dir, window_days=7)
    all_time = build_report(data_dir=data_dir, window_days=0)
    assert "0 save · 0 read · 0 skip · 0 note" in windowed
    assert "1 save · 0 read · 0 skip · 0 note" in all_time


def test_report_never_mutates_files(data_dir):
    now = datetime.now(timezone.utc)
    ix_path = data_dir / "interactions.jsonl"
    _write_jsonl(ix_path, [{"kind": "save", "item_id": "p1", "at": now.isoformat()}])
    _write_jsonl(data_dir / "recommendations.jsonl", [])
    _write_jsonl(data_dir / "candidates.jsonl", [])
    _write_json(data_dir / "state.json", {})
    _write_json(data_dir / "topics.json", {"topics": []})

    before = ix_path.read_bytes()
    build_report(data_dir=data_dir, window_days=7, now=now)
    build_report(data_dir=data_dir, window_days=7, now=now)
    assert ix_path.read_bytes() == before


def test_accepts_legacy_type_created_at_fields(data_dir):
    """research_copilot.commands._record_action writes {"type", "created_at"};
    the report must count those as well as the canonical {"kind", "at"} shape."""
    now = datetime.now(timezone.utc)
    _write_jsonl(
        data_dir / "interactions.jsonl",
        [
            # Canonical shape.
            {"kind": "save", "item_id": "p1", "at": now.isoformat()},
            # Legacy shape written by the existing CLI/gateway handlers.
            {"type": "save", "item_id": "p2", "created_at": now.isoformat()},
            {"type": "skip", "item_id": "p3", "created_at": now.isoformat()},
        ],
    )
    _write_jsonl(data_dir / "recommendations.jsonl", [])
    _write_jsonl(data_dir / "candidates.jsonl", [])
    _write_json(data_dir / "state.json", {})
    _write_json(data_dir / "topics.json", {"topics": []})

    report = build_report(data_dir=data_dir, window_days=7, now=now)
    assert "2 save · 0 read · 1 skip · 0 note" in report


@pytest.mark.parametrize(
    "recommended_at",
    [
        "2026-07-16",
        "2026-07-16T08:30:00",
        "2026-07-16T08:30:00Z",
        "2026-07-16T16:30:00+08:00",
    ],
)
def test_recommendation_dates_are_normalized_to_utc(data_dir, recommended_at):
    """Historical recommendation records mix dates and timezone formats."""
    now = datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc)
    _write_jsonl(
        data_dir / "recommendations.jsonl",
        [{"id": "p1", "recommended_at": recommended_at, "title": "T1"}],
    )
    _write_jsonl(data_dir / "interactions.jsonl", [])
    _write_jsonl(data_dir / "candidates.jsonl", [])
    _write_json(data_dir / "state.json", {})
    _write_json(data_dir / "topics.json", {"topics": []})

    report = build_report(data_dir=data_dir, window_days=7, now=now)
    assert "Recommendations delivered: 1" in report
