from __future__ import annotations

import urllib.error
from datetime import datetime, timezone

import pytest

from research_copilot.sources import ProviderError, SourceBudget, SourceDefinition
from research_copilot.sources.providers import build_provider_registry
from research_copilot.sources.providers.base import FetchContext
from research_copilot.sources.providers.rss import RssProvider


ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>First &amp; Best</title>
    <link rel="alternate" href="https://example.com/first" />
    <summary><![CDATA[<p>A <b>useful</b> summary.</p>]]></summary>
    <updated>2026-07-17T08:00:00+08:00</updated>
  </entry>
  <entry>
    <title>Second</title>
    <link href="https://example.com/second" />
  </entry>
</feed>
"""

RSS = b"""<?xml version="1.0"?>
<rss><channel>
  <item><title>RSS item</title><link>https://example.com/rss</link><description>Body</description></item>
</channel></rss>
"""


def _source(**options):
    return SourceDefinition(
        id="feed", provider="rss", display_name="Feed", source_type="lab_blog",
        enabled=True, tier=0.9, topics=("*",), options=options,
        budget=SourceBudget(max_requests=2, max_items=10, max_items_per_topic=5),
    )


def _context(*, requests=1, items=10):
    return FetchContext(
        started_at=datetime.now(timezone.utc), active_topic_ids=(),
        remaining_requests=requests, remaining_items=items,
    )


def test_atom_contract_normalizes_content_date_and_limit():
    calls = []
    provider = RssProvider(fetcher=lambda url, timeout: calls.append((url, timeout)) or ATOM)
    result = provider.fetch(_source(feed_url="https://example.com/feed", timeout_seconds=7), _context(items=1))
    assert result.requests == 1
    assert len(result.items) == 1
    item = result.items[0].item
    assert item.title == "First & Best"
    assert item.summary == "A useful summary."
    assert item.published_at == "2026-07-17T00:00:00+00:00"
    assert calls == [("https://example.com/feed", 7)]


def test_rss_contract_parses_channel_items():
    result = RssProvider(fetcher=lambda *_: RSS).fetch(
        _source(feed_url="https://example.com/rss"), _context(),
    )
    assert result.items[0].item.title == "RSS item"
    assert result.items[0].item.url == "https://example.com/rss"


def test_malformed_xml_is_non_retryable_parse_error():
    with pytest.raises(ProviderError) as captured:
        RssProvider(fetcher=lambda *_: b"<broken").fetch(
            _source(feed_url="https://example.com/feed"), _context(),
        )
    assert captured.value.code == "parse_error"
    assert captured.value.requests == 1
    assert captured.value.retryable is False


def test_http_429_preserves_rate_limit_and_request_count():
    def fail(url, timeout):
        raise urllib.error.HTTPError(url, 429, "limited", {}, None)

    with pytest.raises(ProviderError) as captured:
        RssProvider(fetcher=fail).fetch(_source(feed_url="https://example.com/feed"), _context())
    assert captured.value.code == "http_429"
    assert captured.value.requests == 1
    assert captured.value.retryable is True
    assert captured.value.rate_limited is True


def test_zero_request_budget_never_calls_network():
    calls = []
    with pytest.raises(ProviderError, match="budget is exhausted") as captured:
        RssProvider(fetcher=lambda *_: calls.append(True) or RSS).fetch(
            _source(feed_url="https://example.com/feed"), _context(requests=0),
        )
    assert captured.value.code == "budget_exhausted"
    assert calls == []


def test_builtin_registry_declares_strict_rss_options():
    registry = build_provider_registry()
    assert "rss" in registry.ids()
    spec = registry.option_specs()["rss"]
    assert spec.required == {"feed_url"}
    assert "timeout_seconds" in spec.allowed
