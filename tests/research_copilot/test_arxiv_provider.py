from __future__ import annotations

import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from research_copilot.sources import ProviderError, SourceDefinition
from research_copilot.sources.providers.arxiv import ArxivProvider
from research_copilot.sources.providers.base import FetchContext


ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2607.01234v2</id>
    <title>  A Long\nTitle  </title>
    <summary>Useful abstract.</summary>
    <published>2026-07-17T00:00:00Z</published>
    <author><name>Alice</name></author>
  </entry>
</feed>
"""


def _source(**options):
    return SourceDefinition(
        id="arxiv", provider="arxiv", display_name="arXiv",
        source_type="preprint_index", enabled=False, tier=0.5,
        topics=("*",), options=options,
    )


def _context(query="speculative decoding", requests=2, items=5):
    return FetchContext(
        datetime.now(timezone.utc), ("inference",), requests, items,
        topic_queries={"inference": (query,)},
    )


def test_arxiv_contract_phrase_quotes_and_parses_atom():
    calls = []
    result = ArxivProvider(fetcher=lambda url, timeout: calls.append(url) or ATOM).fetch(
        _source(max_results_per_query=3), _context(),
    )
    item = result.items[0]
    assert item.item.arxiv_id == "2607.01234"
    assert item.item.title == "A Long Title"
    assert item.item.authors == ("Alice",)
    assert item.topics[0].topic_id == "inference"
    query = parse_qs(urlsplit(calls[0]).query)
    assert query["search_query"] == ['all:"speculative decoding"']
    assert query["max_results"] == ["3"]


def test_arxiv_single_word_is_not_phrase_quoted():
    calls = []
    ArxivProvider(fetcher=lambda url, timeout: calls.append(url) or b'<feed xmlns="http://www.w3.org/2005/Atom"/>').fetch(
        _source(), _context(query="vLLM"),
    )
    assert parse_qs(urlsplit(calls[0]).query)["search_query"] == ["all:vLLM"]


def test_arxiv_respects_request_budget_across_queries():
    calls = []
    context = FetchContext(
        datetime.now(timezone.utc), ("a", "b"), 1, 5,
        topic_queries={"a": ("one",), "b": ("two",)},
    )
    result = ArxivProvider(fetcher=lambda *args: calls.append(args) or b'<feed xmlns="http://www.w3.org/2005/Atom"/>').fetch(
        _source(), context,
    )
    assert result.requests == 1
    assert len(calls) == 1


def test_arxiv_429_is_retryable_and_accounted():
    def fail(url, timeout):
        raise urllib.error.HTTPError(url, 429, "limited", {}, None)
    with pytest.raises(ProviderError) as captured:
        ArxivProvider(fetcher=fail).fetch(_source(), _context())
    assert captured.value.code == "http_429"
    assert captured.value.requests == 1
    assert captured.value.rate_limited is True


def test_arxiv_invalid_xml_is_parse_error():
    with pytest.raises(ProviderError) as captured:
        ArxivProvider(fetcher=lambda *_: b"<broken").fetch(_source(), _context())
    assert captured.value.code == "parse_error"
    assert captured.value.requests == 1
