from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from research_copilot.sources import ProviderError, SourceDefinition
from research_copilot.sources.providers.base import FetchContext
from research_copilot.sources.providers.semantic_scholar import SemanticScholarProvider


def _source(**options):
    return SourceDefinition(
        id="s2", provider="semantic_scholar", display_name="S2",
        source_type="academic_index", enabled=True, tier=0.85,
        topics=("*",), options=options,
    )


def _context(requests=3, items=5):
    return FetchContext(
        started_at=datetime.now(timezone.utc),
        active_topic_ids=("research-agent",),
        remaining_requests=requests,
        remaining_items=items,
        topic_queries={"research-agent": ("deep research agent",)},
    )


def test_semantic_scholar_contract_maps_identifiers_topics_and_headers():
    calls = []
    payload = {"data": [{
        "paperId": "S2-ID", "title": "Agent Paper", "url": "",
        "abstract": "Abstract", "publicationDate": "2026-07-01",
        "externalIds": {"ArXiv": "2607.00001", "DOI": "10.1/example"},
        "authors": [{"name": "Alice"}],
    }]}
    def fetch(url, timeout, headers):
        calls.append((url, timeout, headers))
        return json.dumps(payload).encode()
    result = SemanticScholarProvider(api_key="secret", fetcher=fetch).fetch(
        _source(timeout_seconds=7, limit_per_query=4), _context(),
    )
    item = result.items[0]
    assert item.item.arxiv_id == "2607.00001"
    assert item.item.doi == "10.1/example"
    assert item.item.semantic_scholar_id == "S2-ID"
    assert item.item.url == "https://arxiv.org/abs/2607.00001"
    assert item.topics[0].topic_id == "research-agent"
    query = parse_qs(urlsplit(calls[0][0]).query)
    assert query["query"] == ["deep research agent"]
    assert calls[0][2]["x-api-key"] == "secret"


def test_semantic_scholar_stops_at_request_budget():
    calls = []
    context = FetchContext(
        started_at=datetime.now(timezone.utc), active_topic_ids=("a", "b"),
        remaining_requests=1, remaining_items=10,
        topic_queries={"a": ("one",), "b": ("two",)},
    )
    result = SemanticScholarProvider(fetcher=lambda *args: calls.append(args) or b'{"data": []}').fetch(
        _source(), context,
    )
    assert result.requests == 1
    assert len(calls) == 1


def test_semantic_scholar_429_is_structured():
    def fail(url, timeout, headers):
        raise urllib.error.HTTPError(url, 429, "limited", {}, None)
    with pytest.raises(ProviderError) as captured:
        SemanticScholarProvider(fetcher=fail).fetch(_source(), _context())
    assert captured.value.code == "http_429"
    assert captured.value.requests == 1
    assert captured.value.retryable is True


def test_semantic_scholar_returns_empty_without_queries():
    result = SemanticScholarProvider(fetcher=lambda *_: pytest.fail("network called")).fetch(
        _source(),
        FetchContext(datetime.now(timezone.utc), (), 2, 5),
    )
    assert result == result.__class__()
