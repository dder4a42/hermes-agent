"""Catalog and provider contracts for Research Copilot sources."""

from .catalog import SourceCatalog, SourceCatalogError, load_source_catalog
from .models import (
    CollectionBudget,
    ProviderOptionSpec,
    RetryPolicy,
    SourceBudget,
    SourceDefinition,
)
from .providers.base import FetchContext, ProviderError, ProviderResult, SourceProvider
from .registry import ProviderRegistry

__all__ = [
    "FetchContext",
    "CollectionBudget",
    "ProviderRegistry",
    "ProviderError",
    "ProviderResult",
    "ProviderOptionSpec",
    "RetryPolicy",
    "SourceBudget",
    "SourceCatalog",
    "SourceCatalogError",
    "SourceDefinition",
    "SourceProvider",
    "SourceRunner",
    "CollectionSummary",
    "load_source_catalog",
]

from .runner import CollectionSummary, SourceRunner
