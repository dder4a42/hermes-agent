from __future__ import annotations

import urllib.error
from datetime import datetime, timezone

import pytest

from research_copilot.sources import ProviderError, SourceBudget, SourceDefinition
from research_copilot.sources.providers import build_provider_registry
from research_copilot.sources.providers.base import FetchContext
from research_copilot.sources.providers.rss import RssProvider
from research_copilot.net import HttpResponse


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

RICH_RSS = b"""<?xml version="1.0"?>
<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:feedburner="http://rssnamespace.org/feedburner/ext/1.0">
  <channel><item>
    <title><![CDATA[Agent <b>Evaluation</b> Update]]></title>
    <link>https://tracker.example.com/post?utm_source=rss</link>
    <feedburner:origLink>https://example.com/post?utm_source=rss</feedburner:origLink>
    <description>Short teaser.</description>
    <content:encoded><![CDATA[<article>Full research agent evaluation and tool-use trajectory evidence.</article>]]></content:encoded>
    <dc:creator>Alice Researcher</dc:creator>
    <dc:date>Fri, 08 Aug 2026 09:30:00 +0800</dc:date>
    <category>Agents</category><category>Evaluation</category>
  </item></channel>
</rss>
"""

ATOM_03 = b"""<?xml version="1.0"?>
<feed xmlns="http://purl.org/atom/ns#">
  <entry><title>Old Atom</title><link rel="alternate" href="/posts/old" />
  <content>Useful full text</content></entry>
</feed>
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


def test_rss_parses_namespaced_full_content_provenance_and_rfc822_date():
    result = RssProvider(fetcher=lambda *_: RICH_RSS).fetch(
        _source(feed_url="https://feed.example.com/rss"), _context(),
    )
    item = result.items[0].item
    assert item.title == "Agent Evaluation Update"
    assert item.url == "https://example.com/post"
    assert item.summary == "Full research agent evaluation and tool-use trajectory evidence."
    assert item.authors == ("Alice Researcher",)
    assert item.published_at == "2026-08-08T01:30:00+00:00"
    assert item.metadata == {"feed_categories": ("Agents", "Evaluation")}
    assert result.metrics == {
        "feed_format": "rss", "parsed_entry_count": 1,
        "emitted_item_count": 1, "missing_title_count": 0,
        "missing_link_count": 0, "duplicate_link_count": 0,
        "full_content_count": 1,
    }


def test_old_atom_namespace_and_relative_links_are_supported():
    result = RssProvider(fetcher=lambda *_: ATOM_03).fetch(
        _source(feed_url="https://example.com/feed.xml"), _context(),
    )
    assert result.items[0].item.url == "https://example.com/posts/old"
    assert result.items[0].item.summary == "Useful full text"
    assert result.metrics["feed_format"] == "atom"


def test_rss_guid_permalink_is_used_when_link_is_missing():
    payload = b"""<rss><channel><item><title>GUID item</title>
      <guid isPermaLink="true">https://example.com/guid?utm_medium=rss</guid>
      <description>Body</description></item></channel></rss>"""
    result = RssProvider(fetcher=lambda *_: payload).fetch(
        _source(feed_url="https://example.com/feed"), _context(),
    )
    assert result.items[0].item.url == "https://example.com/guid"


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


def test_rss_uses_conditional_headers_and_persists_response_validators():
    calls = []

    def fetch(url, timeout, headers):
        calls.append((url, timeout, headers))
        return HttpResponse(
            body=RSS, status=200,
            headers={"ETag": '"feed-v2"', "Last-Modified": "Fri, 08 Aug 2026 01:00:00 GMT"},
        )

    context = FetchContext(
        started_at=datetime.now(timezone.utc), active_topic_ids=(),
        remaining_requests=1, remaining_items=10,
        source_state={"etag": '"feed-v1"', "last_modified": "Thu, 07 Aug 2026 01:00:00 GMT"},
    )
    result = RssProvider(fetcher=fetch).fetch(
        _source(feed_url="https://example.com/rss"), context,
    )
    assert calls[0][2] == {
        "If-None-Match": '"feed-v1"',
        "If-Modified-Since": "Thu, 07 Aug 2026 01:00:00 GMT",
    }
    assert result.state_updates == {
        "etag": '"feed-v2"',
        "last_modified": "Fri, 08 Aug 2026 01:00:00 GMT",
    }


def test_rss_304_is_a_successful_empty_increment():
    result = RssProvider(
        fetcher=lambda *_: HttpResponse(body=b"", status=304, headers={})
    ).fetch(_source(feed_url="https://example.com/rss"), _context())
    assert result.requests == 1
    assert result.items == ()
