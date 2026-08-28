from __future__ import annotations

import urllib.error
from datetime import datetime, timezone

import pytest

from research_copilot.sources import ProviderError, SourceDefinition
from research_copilot.sources.providers.base import FetchContext
from research_copilot.sources.providers.github_trending import GitHubTrendingProvider


HTML = b"""
<article class="Box-row">
  <h2><a href="/org/agent-runtime"> org / agent-runtime </a></h2>
  <p class="col-9">An <b>LLM agent</b> runtime.</p>
  <span>1,234 stars today</span>
</article>
<article class="Box-row">
  <h2><a href="/org/other">other</a></h2>
  <p>Another project</p>
</article>
"""


def _source(**options):
    return SourceDefinition(
        id="github", provider="github_trending", display_name="GitHub Trending",
        source_type="community_signal", enabled=True, tier=0.8,
        topics=("*",), options=options,
    )


def _context(requests=2, items=10):
    return FetchContext(datetime.now(timezone.utc), (), requests, items)


def test_github_contract_parses_repository_and_stars():
    calls = []
    result = GitHubTrendingProvider(fetcher=lambda url, timeout: calls.append(url) or HTML).fetch(
        _source(languages=["python"], since="daily"), _context(),
    )
    assert result.requests == 1
    item = result.items[0]
    assert item.item.title == "agent-runtime"
    assert item.item.url == "https://github.com/org/agent-runtime"
    assert item.item.authors == ("org",)
    assert item.metadata["stars_in_period"] == 1234
    assert calls == ["https://github.com/trending/python?since=daily"]


def test_github_respects_request_budget_across_languages():
    calls = []
    result = GitHubTrendingProvider(fetcher=lambda *args: calls.append(args) or b"").fetch(
        _source(languages=["python", "rust"]), _context(requests=1),
    )
    assert result.requests == 1
    assert len(calls) == 1


def test_github_rejects_invalid_period_without_network():
    with pytest.raises(ProviderError) as captured:
        GitHubTrendingProvider(fetcher=lambda *_: pytest.fail("network called")).fetch(
            _source(since="yearly"), _context(),
        )
    assert captured.value.code == "invalid_config"


def test_github_429_is_structured():
    def fail(url, timeout):
        raise urllib.error.HTTPError(url, 429, "limited", {}, None)
    with pytest.raises(ProviderError) as captured:
        GitHubTrendingProvider(fetcher=fail).fetch(_source(), _context())
    assert captured.value.requests == 1
    assert captured.value.rate_limited is True
