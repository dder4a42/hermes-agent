from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest

from research_copilot.sources import ProviderError, SourceDefinition
from research_copilot.sources.providers.base import FetchContext
from research_copilot.sources.providers.tavily import TavilyProvider


def _source(**options):
    return SourceDefinition(
        id="tavily", provider="tavily", display_name="Tavily",
        source_type="web_search", enabled=True, tier=0.7,
        topics=("*",), options=options,
    )


def _context(requests=2, items=5):
    return FetchContext(
        datetime.now(timezone.utc), ("research-agent",), requests, items,
        topic_queries={"research-agent": ("deep research agent",)},
    )


def test_tavily_contract_uses_secret_and_catalog_domains():
    calls = []
    response = {"results": [{
        "title": "Agent report", "url": "https://example.com/report",
        "content": "Evidence", "score": 0.9,
    }]}
    def fetch(url, payload, timeout, headers):
        calls.append((url, json.loads(payload), timeout, headers))
        return json.dumps(response).encode()
    result = TavilyProvider(api_key="secret", fetcher=fetch).fetch(
        _source(domains=["example.com"], timeout_seconds=8), _context(),
    )
    assert result.requests == 1
    assert result.items[0].topics[0].topic_id == "research-agent"
    assert result.items[0].rank == 1
    body = calls[0][1]
    assert body["api_key"] == "secret"
    assert body["include_domains"] == ["example.com"]
    assert body["query"] == "deep research agent"


def test_tavily_missing_key_never_calls_network():
    with pytest.raises(ProviderError) as captured:
        TavilyProvider(api_key="", fetcher=lambda *_: pytest.fail("network called")).fetch(
            _source(), _context(),
        )
    assert captured.value.code == "missing_credential"
    assert captured.value.requests == 0


def test_tavily_rejects_non_list_domains():
    with pytest.raises(ProviderError) as captured:
        TavilyProvider(api_key="x").fetch(_source(domains="example.com"), _context())
    assert captured.value.code == "invalid_config"


def test_tavily_429_is_retryable_and_accounted():
    def fail(*args):
        raise urllib.error.HTTPError(args[0], 429, "limited", {}, None)
    with pytest.raises(ProviderError) as captured:
        TavilyProvider(api_key="x", fetcher=fail).fetch(_source(), _context())
    assert captured.value.code == "http_429"
    assert captured.value.requests == 1
    assert captured.value.rate_limited is True


def test_tavily_stops_at_global_request_budget():
    calls = []
    context = FetchContext(
        datetime.now(timezone.utc), ("a", "b"), 1, 5,
        topic_queries={"a": ("one",), "b": ("two",)},
    )
    result = TavilyProvider(
        api_key="x",
        fetcher=lambda *args: calls.append(args) or b'{"results": []}',
    ).fetch(_source(), context)
    assert result.requests == 1
    assert len(calls) == 1
