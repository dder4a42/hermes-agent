from __future__ import annotations

import textwrap

import pytest

from research_copilot.sources import (
    ProviderOptionSpec,
    ProviderRegistry,
    SourceCatalogError,
    load_source_catalog,
)


def _write(tmp_path, body: str):
    path = tmp_path / "sources.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _load(path):
    return load_source_catalog(
        path,
        provider_ids={"rss", "semantic_scholar"},
        topic_ids={"research-agent", "inference"},
    )


def test_loads_strict_catalog(tmp_path):
    catalog = _load(_write(tmp_path, """
        schema_version: 1
        sources:
          - id: openai-news
            provider: rss
            display_name: OpenAI News
            type: company_blog
            enabled: true
            tier: 1.0
            topics: [research-agent]
            budget:
              max_requests: 2
              max_items: 20
              max_items_per_topic: 5
            retry:
              max_attempts: 3
              backoff_seconds: 2.5
            options:
              feed_url: https://example.com/feed.xml
    """))
    source = catalog.by_id("openai-news")
    assert source.provider == "rss"
    assert source.budget.max_items == 20
    assert source.retry.max_attempts == 3
    assert source.options["feed_url"].endswith("feed.xml")
    with pytest.raises(TypeError):
        source.options["feed_url"] = "changed"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("schema_version: 2\nsources: []", "Unsupported Source Catalog schema"),
        ("schema_version: 1\nsources: []\nextra: true", "Unknown fields at catalog"),
        ("""
            schema_version: 1
            sources:
              - id: x
                provider: missing
                display_name: X
                type: feed
        """, "Unknown provider"),
        ("""
            schema_version: 1
            sources:
              - id: x
                provider: rss
                display_name: X
                type: feed
                topics: [unknown]
        """, "Unknown topics"),
        ("""
            schema_version: 1
            sources:
              - id: x
                provider: rss
                display_name: X
                type: feed
                budget: {max_items: -1}
        """, "cannot be negative"),
    ],
)
def test_rejects_invalid_catalogs(tmp_path, body, message):
    with pytest.raises(SourceCatalogError, match=message):
        _load(_write(tmp_path, body))


def test_rejects_duplicate_source_ids(tmp_path):
    with pytest.raises(SourceCatalogError, match="Duplicate source id"):
        _load(_write(tmp_path, """
            schema_version: 1
            sources:
              - {id: x, provider: rss, display_name: X, type: feed}
              - {id: x, provider: rss, display_name: X2, type: feed}
        """))


def test_provider_registry_rejects_duplicates_and_unknown_ids():
    registry = ProviderRegistry()
    provider = object()
    registry.register("rss", provider)
    assert registry.get("rss") is provider
    assert registry.ids() == {"rss"}
    with pytest.raises(ValueError, match="already registered"):
        registry.register("rss", object())
    with pytest.raises(KeyError, match="Unknown source provider"):
        registry.get("missing")


def test_provider_options_are_strict_and_required(tmp_path):
    path = _write(tmp_path, """
        schema_version: 1
        sources:
          - id: feed
            provider: rss
            display_name: Feed
            type: feed
            options:
              command: curl example.com
    """)
    specs = {"rss": ProviderOptionSpec(allowed=frozenset({"feed_url"}), required=frozenset({"feed_url"}))}
    with pytest.raises(SourceCatalogError, match="Unknown fields.*command"):
        load_source_catalog(path, provider_ids={"rss"}, topic_ids=set(), option_specs=specs)

    path = _write(tmp_path, """
        schema_version: 1
        sources:
          - id: feed
            provider: rss
            display_name: Feed
            type: feed
    """)
    with pytest.raises(SourceCatalogError, match="Missing required options.*feed_url"):
        load_source_catalog(path, provider_ids={"rss"}, topic_ids=set(), option_specs=specs)


def test_registry_exposes_option_specs():
    registry = ProviderRegistry()
    spec = ProviderOptionSpec(allowed=frozenset({"feed_url"}), required=frozenset({"feed_url"}))
    registry.register("rss", object(), option_spec=spec)
    assert registry.option_specs() == {"rss": spec}
