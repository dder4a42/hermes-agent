from __future__ import annotations

import urllib.error
from datetime import datetime, timezone

import pytest

from research_copilot.sources import ProviderError, SourceDefinition
from research_copilot.sources.providers.alphaxiv import AlphaXivProvider
from research_copilot.sources.providers.base import FetchContext


HTML = b"""
<a href="https://alphaxiv.org/abs/2607.01234v2"><span>Agent Paper</span></a>
<a href="/p/2607.01234">duplicate</a>
<a href="/p/2607.09999">Second Paper</a>
"""


def _source(**options):
    return SourceDefinition(
        id="alphaxiv", provider="alphaxiv", display_name="AlphaXiv",
        source_type="community_discussion", enabled=True, tier=0.85,
        topics=("*",), options=options,
    )


def _context(requests=1, items=10):
    return FetchContext(datetime.now(timezone.utc), (), requests, items)


def test_alphaxiv_contract_normalizes_and_deduplicates_arxiv_ids():
    result = AlphaXivProvider(fetcher=lambda *_: HTML).fetch(_source(), _context())
    assert result.requests == 1
    assert [item.item.arxiv_id for item in result.items] == ["2607.01234", "2607.09999"]
    assert result.items[0].item.title == "Agent Paper"
    assert result.items[0].item.metadata["discussion_url"].endswith("2607.01234")


def test_alphaxiv_respects_item_budget():
    result = AlphaXivProvider(fetcher=lambda *_: HTML).fetch(_source(), _context(items=1))
    assert len(result.items) == 1


def test_alphaxiv_zero_budget_does_not_call_network():
    with pytest.raises(ProviderError) as captured:
        AlphaXivProvider(fetcher=lambda *_: pytest.fail("network called")).fetch(
            _source(), _context(requests=0),
        )
    assert captured.value.code == "budget_exhausted"


def test_alphaxiv_500_is_retryable():
    def fail(url, timeout):
        raise urllib.error.HTTPError(url, 500, "bad", {}, None)
    with pytest.raises(ProviderError) as captured:
        AlphaXivProvider(fetcher=fail).fetch(_source(), _context())
    assert captured.value.requests == 1
    assert captured.value.retryable is True
