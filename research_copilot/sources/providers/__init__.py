"""Bundled Research Copilot source providers."""

import os

from research_copilot.sources.models import ProviderOptionSpec
from research_copilot.sources.registry import ProviderRegistry

from .base import FetchContext, ProviderError, ProviderResult, SourceProvider
from .alphaxiv import AlphaXivProvider
from .arxiv import ArxivProvider
from .github_trending import GitHubTrendingProvider
from .gmail_newsletter import GmailNewsletterProvider
from .huggingface import HuggingFaceDailyProvider
from .rss import RssProvider
from .semantic_scholar import SemanticScholarProvider
from .tavily import TavilyProvider

__all__ = [
    "FetchContext",
    "AlphaXivProvider",
    "ArxivProvider",
    "GitHubTrendingProvider",
    "GmailNewsletterProvider",
    "ProviderError",
    "ProviderResult",
    "HuggingFaceDailyProvider",
    "RssProvider",
    "SemanticScholarProvider",
    "TavilyProvider",
    "SourceProvider",
    "build_provider_registry",
]


def build_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        "rss",
        RssProvider(),
        option_spec=ProviderOptionSpec(
            allowed=frozenset({"feed_url", "timeout_seconds"}),
            required=frozenset({"feed_url"}),
        ),
    )
    registry.register(
        "semantic_scholar",
        SemanticScholarProvider(),
        option_spec=ProviderOptionSpec(
            allowed=frozenset({"timeout_seconds", "limit_per_query"}),
        ),
    )
    registry.register(
        "huggingface_daily",
        HuggingFaceDailyProvider(),
        option_spec=ProviderOptionSpec(
            allowed=frozenset({"endpoint", "timeout_seconds"}),
        ),
    )
    registry.register(
        "tavily",
        TavilyProvider(api_key=os.environ.get("TAVILY_API_KEY", "")),
        option_spec=ProviderOptionSpec(
            allowed=frozenset({"domains", "search_depth", "timeout_seconds"}),
        ),
    )
    registry.register(
        "github_trending",
        GitHubTrendingProvider(),
        option_spec=ProviderOptionSpec(
            allowed=frozenset({"languages", "since", "timeout_seconds"}),
        ),
    )
    registry.register(
        "alphaxiv",
        AlphaXivProvider(),
        option_spec=ProviderOptionSpec(
            allowed=frozenset({"endpoint", "timeout_seconds"}),
        ),
    )
    registry.register(
        "arxiv",
        ArxivProvider(),
        option_spec=ProviderOptionSpec(
            allowed=frozenset({"max_results_per_query", "timeout_seconds"}),
        ),
    )
    registry.register(
        "gmail_newsletter",
        GmailNewsletterProvider(
            address=os.environ.get("RESEARCH_COPILOT_GMAIL_ADDR", ""),
            app_password=os.environ.get("GMAIL_APP_PASSWORD", ""),
        ),
        option_spec=ProviderOptionSpec(
            allowed=frozenset({
                "host", "port", "label", "proxy_url",
                "lookback_days", "max_messages",
            }),
        ),
    )
    return registry
