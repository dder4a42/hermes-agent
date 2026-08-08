from __future__ import annotations

from datetime import datetime, timezone

from research_copilot.sources.providers.base import FetchContext
from research_copilot.sources.query_plan import build_query_plan


def _context(*, day: int = 8, requests: int = 10):
    return FetchContext(
        started_at=datetime(2026, 8, day, tzinfo=timezone.utc),
        active_topic_ids=("a", "b", "c"),
        remaining_requests=requests,
        remaining_items=30,
        topic_queries={
            "a": ("a-one", "shared query", "a-three"),
            "b": ("b-one", "b-two"),
            "c": ("c-one", "SHARED   QUERY"),
        },
    )


def test_query_plan_round_robins_before_second_query():
    plan = build_query_plan(_context(), source_id="search")
    first_round = plan[:3]
    assert {entry.topic_ids[0] for entry in first_round} == {"a", "b", "c"}


def test_query_plan_deduplicates_requests_and_merges_topic_attribution():
    plan = build_query_plan(_context(), source_id="search")
    shared = [entry for entry in plan if entry.query.casefold() == "shared query"]
    assert len(shared) == 1
    assert set(shared[0].topic_ids) == {"a", "c"}


def test_query_plan_is_bounded_and_deterministic_for_a_run():
    first = build_query_plan(_context(requests=2), source_id="search")
    second = build_query_plan(_context(requests=2), source_id="search")
    assert first == second
    assert len(first) == 2


def test_query_plan_rotates_topic_order_across_days():
    orders = {
        tuple(entry.topic_ids[0] for entry in build_query_plan(
            _context(day=day, requests=3), source_id="search",
        ))
        for day in range(1, 10)
    }
    assert len(orders) > 1
