"""Fair, deterministic query planning shared by search-backed providers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from .providers.base import FetchContext


@dataclass(frozen=True)
class QueryPlanEntry:
    query: str
    topic_ids: tuple[str, ...]


def build_query_plan(
    context: FetchContext,
    *,
    source_id: str,
) -> tuple[QueryPlanEntry, ...]:
    """Round-robin topics, rotate daily, and merge duplicate query requests."""
    topics = list(dict.fromkeys(context.active_topic_ids))
    if not topics or context.remaining_requests <= 0:
        return ()
    seed = f"{source_id}:{context.started_at.date().isoformat()}".encode("utf-8")
    offset = int.from_bytes(hashlib.sha256(seed).digest()[:4], "big") % len(topics)
    topics = topics[offset:] + topics[:offset]
    per_topic: dict[str, tuple[str, ...]] = {}
    for topic_id in topics:
        seen: set[str] = set()
        values: list[str] = []
        for raw in context.topic_queries.get(topic_id, ()):
            query = raw.strip()
            normalized = " ".join(query.casefold().split())
            if query and normalized not in seen:
                seen.add(normalized)
                values.append(query)
        per_topic[topic_id] = tuple(values)

    planned: list[QueryPlanEntry] = []
    positions: dict[str, int] = {}
    max_depth = max((len(values) for values in per_topic.values()), default=0)
    for depth in range(max_depth):
        for topic_id in topics:
            values = per_topic[topic_id]
            if depth >= len(values):
                continue
            query = values[depth]
            normalized = " ".join(query.casefold().split())
            if normalized in positions:
                index = positions[normalized]
                existing = planned[index]
                planned[index] = QueryPlanEntry(
                    existing.query,
                    tuple(dict.fromkeys((*existing.topic_ids, topic_id))),
                )
            else:
                positions[normalized] = len(planned)
                planned.append(QueryPlanEntry(query, (topic_id,)))
    return tuple(planned[: context.remaining_requests])
