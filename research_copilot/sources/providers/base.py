"""Narrow provider contract: network and parsing, never persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol

from research_copilot.library.models import ResearchItemDraft, TopicMatch
from research_copilot.sources.models import SourceDefinition


@dataclass(frozen=True)
class FetchContext:
    started_at: datetime
    active_topic_ids: tuple[str, ...]
    remaining_requests: int
    remaining_items: int
    topic_queries: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    topic_excludes: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderItem:
    item: ResearchItemDraft
    topics: tuple[TopicMatch, ...] = ()
    query: str | None = None
    rank: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    items: tuple[ProviderItem, ...] = ()
    requests: int = 0
    filtered: int = 0
    rate_limited: bool = False
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.requests < 0 or self.filtered < 0:
            raise ValueError("Provider result counts cannot be negative")


class ProviderError(RuntimeError):
    """A provider failure that preserves request accounting and retry intent."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        requests: int = 0,
        retryable: bool = False,
        rate_limited: bool = False,
    ) -> None:
        if requests < 0:
            raise ValueError("Provider error request count cannot be negative")
        super().__init__(message)
        self.code = code
        self.requests = requests
        self.retryable = retryable
        self.rate_limited = rate_limited


class SourceProvider(Protocol):
    def fetch(self, source: SourceDefinition, context: FetchContext) -> ProviderResult:
        """Fetch and parse one configured source without writing pipeline state."""
        ...
