"""Domain values shared by the Research Library and source pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ResearchItemDraft:
    title: str
    item_type: str = "research_signal"
    summary: str = ""
    url: str = ""
    authors: tuple[str, ...] = ()
    published_at: str | None = None
    doi: str = ""
    arxiv_id: str = ""
    semantic_scholar_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceEvidence:
    source_id: str
    source_run_id: str | None = None
    query: str | None = None
    rank: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TopicMatch:
    topic_id: str
    confidence: float
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class UpsertResult:
    item_id: str
    canonical_key: str
    disposition: str


@dataclass(frozen=True)
class SourceRunCounts:
    requests: int = 0
    fetched: int = 0
    new: int = 0
    merged: int = 0
    unchanged: int = 0
    filtered: int = 0

    def __post_init__(self) -> None:
        if any(value < 0 for value in self.__dict__.values()):
            raise ValueError("Source run counts cannot be negative")


def utc_iso(value: datetime) -> str:
    """Serialize a datetime as an explicit UTC timestamp."""
    if value.tzinfo is None:
        raise ValueError("Research Library timestamps must be timezone-aware")
    from datetime import timezone

    return value.astimezone(timezone.utc).isoformat()
