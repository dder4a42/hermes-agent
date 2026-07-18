"""Validated, immutable source configuration values."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class SourceBudget:
    max_requests: int = 10
    max_items: int = 50
    max_items_per_topic: int = 10

    def __post_init__(self) -> None:
        if self.max_requests < 0 or self.max_items < 0 or self.max_items_per_topic < 0:
            raise ValueError("Source budgets cannot be negative")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    backoff_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("retry.max_attempts must be at least 1")
        if self.backoff_seconds < 0:
            raise ValueError("retry.backoff_seconds cannot be negative")


@dataclass(frozen=True)
class SourceDefinition:
    id: str
    provider: str
    display_name: str
    source_type: str
    enabled: bool
    tier: float
    topics: tuple[str, ...]
    budget: SourceBudget = SourceBudget()
    retry: RetryPolicy = RetryPolicy()
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or not self.provider or not self.display_name or not self.source_type:
            raise ValueError("Source id, provider, display_name and type are required")
        if not 0 <= self.tier <= 1:
            raise ValueError("Source tier must be between 0 and 1")
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True)
class CollectionBudget:
    max_requests: int = 80
    max_new_items: int = 120

    def __post_init__(self) -> None:
        if self.max_requests < 0 or self.max_new_items < 0:
            raise ValueError("Collection budgets cannot be negative")


@dataclass(frozen=True)
class ProviderOptionSpec:
    allowed: frozenset[str] = frozenset()
    required: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.required <= self.allowed:
            raise ValueError("Required provider options must also be allowed")
