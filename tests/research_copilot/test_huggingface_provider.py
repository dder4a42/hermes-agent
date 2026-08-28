from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone

import pytest

from research_copilot.sources import ProviderError, SourceDefinition
from research_copilot.sources.providers.base import FetchContext
from research_copilot.sources.providers.huggingface import HuggingFaceDailyProvider


def _source(**options):
    return SourceDefinition(
        id="hf", provider="huggingface_daily", display_name="HF Daily",
        source_type="community_curated", enabled=True, tier=1.0,
        topics=("*",), options=options,
    )


def _context(items=10):
    return FetchContext(datetime.now(timezone.utc), (), 1, items)


def test_huggingface_contract_accepts_direct_and_nested_shapes():
    payload = [
        {"id": "2607.00001", "title": "First", "summary": "A", "authors": [{"name": "Alice"}]},
        {"paper": {"id": "2607.00002", "title": "Second", "abstract": "B", "authors": ["Bob"]}},
    ]
    result = HuggingFaceDailyProvider(fetcher=lambda *_: json.dumps(payload).encode()).fetch(
        _source(), _context(),
    )
    assert [item.item.title for item in result.items] == ["First", "Second"]
    assert result.items[0].item.authors == ("Alice",)
    assert result.items[1].item.url == "https://huggingface.co/papers/2607.00002"


def test_huggingface_respects_item_budget():
    payload = [{"id": f"2607.0000{i}", "title": str(i)} for i in range(3)]
    result = HuggingFaceDailyProvider(fetcher=lambda *_: json.dumps(payload).encode()).fetch(
        _source(), _context(items=1),
    )
    assert len(result.items) == 1


def test_huggingface_rejects_non_list_response():
    with pytest.raises(ProviderError) as captured:
        HuggingFaceDailyProvider(fetcher=lambda *_: b'{}').fetch(_source(), _context())
    assert captured.value.code == "parse_error"
    assert captured.value.requests == 1


def test_huggingface_500_is_retryable():
    def fail(url, timeout):
        raise urllib.error.HTTPError(url, 500, "bad", {}, None)
    with pytest.raises(ProviderError) as captured:
        HuggingFaceDailyProvider(fetcher=fail).fetch(_source(), _context())
    assert captured.value.retryable is True
    assert captured.value.requests == 1
