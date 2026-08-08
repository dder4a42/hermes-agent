from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from research_copilot.library import (
    LibraryRepository,
    ResearchItemDraft,
    SourceEvidence,
    connect_library,
    initialize_library,
)
from research_copilot.sources import (
    CollectionBudget,
    FailureCooldownPolicy,
    ProviderRegistry,
    ProviderError,
    ProviderResult,
    SourceCatalog,
    SourceDefinition,
    SourceRunner,
)
from research_copilot.sources.providers.base import ProviderItem


class FakeProvider:
    def __init__(self, result=None, error=None):
        self.result = result or ProviderResult()
        self.error = error
        self.contexts = []

    def fetch(self, source, context):
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.result


class SequenceProvider:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.contexts = []

    def fetch(self, source, context):
        self.contexts.append(context)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def runner_parts(tmp_path):
    connection = connect_library(tmp_path / "library.db")
    initialize_library(connection, migrated_at="2026-07-17T00:00:00+00:00")
    try:
        yield connection, LibraryRepository(connection), ProviderRegistry()
    finally:
        connection.close()


def _source(source_id="one", provider="fake", *, enabled=True, max_requests=3, max_items=2):
    from research_copilot.sources import SourceBudget
    return SourceDefinition(
        id=source_id, provider=provider, display_name=source_id,
        source_type="test", enabled=enabled, tier=0.8, topics=("*",),
        budget=SourceBudget(max_requests=max_requests, max_items=max_items, max_items_per_topic=2),
    )


def _items(count):
    return tuple(
        ProviderItem(ResearchItemDraft(title=f"Paper {index}", url=f"https://example.com/{index}"))
        for index in range(count)
    )


def test_runner_enforces_item_budget_and_persists_run(runner_parts):
    connection, repository, registry = runner_parts
    provider = FakeProvider(ProviderResult(
        items=_items(4), requests=2, metrics={"incremental": True},
    ))
    registry.register("fake", provider)
    runner = SourceRunner(providers=registry, repository=repository)
    summary = runner.collect(
        SourceCatalog(1, (_source(max_items=2),)),
        active_topic_ids=(),
        started_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        run_metadata={"profile_revision": "abc"},
    )
    source = summary.sources[0]
    assert (source.fetched, source.new, source.filtered) == (4, 2, 2)
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 2
    run = connection.execute("SELECT * FROM source_runs").fetchone()
    assert run["status"] == "success"
    assert (run["fetched_count"], run["new_count"], run["filtered_count"]) == (4, 2, 2)
    assert json.loads(run["metrics_json"])["profile_revision"] == "abc"
    assert json.loads(run["metrics_json"])["incremental"] is True
    assert provider.contexts[0].remaining_items == 2


def test_dry_run_never_mutates_database(runner_parts):
    connection, repository, registry = runner_parts
    registry.register("fake", FakeProvider(ProviderResult(items=_items(2), requests=1)))
    summary = SourceRunner(providers=registry, repository=repository).collect(
        SourceCatalog(1, (_source(),)),
        active_topic_ids=(), started_at=datetime.now(timezone.utc), dry_run=True,
    )
    assert summary.dry_run is True
    assert summary.new == 2
    assert [preview.title for preview in summary.sources[0].previews] == ["Paper 0", "Paper 1"]
    assert all(preview.disposition == "new" for preview in summary.sources[0].previews)
    for table in ("sources", "source_runs", "research_items", "newsletter_issues", "newsletter_entries"):
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_persisted_gmail_collection_stages_issue_before_library_filtering(runner_parts):
    connection, repository, registry = runner_parts
    metadata = {
        "newsletter_body_hash": "body-1", "newsletter_label": "ResearchFeeds",
        "newsletter_uid": "42", "newsletter_message_id": "<m1>",
        "newsletter_uid_validity": "777",
        "newsletter_sender": "news@example.com", "newsletter_subject": "Issue",
        "newsletter_parser": "generic-v1", "newsletter_tracked_url": "https://track/x",
        "classification_confidence": .8,
    }
    item = ProviderItem(ResearchItemDraft(
        title="Deep research agent launch", url="https://example.com/launch",
        item_type="product_release", metadata=metadata,
    ))
    registry.register("gmail_newsletter", FakeProvider(ProviderResult(items=(item,), requests=1)))
    summary = SourceRunner(providers=registry, repository=repository).collect(
        SourceCatalog(1, (_source(provider="gmail_newsletter", max_items=10),)),
        active_topic_ids=("research-agent",),
        topic_queries={"research-agent": ("deep research agent",)},
        started_at=datetime.now(timezone.utc),
    )
    assert summary.sources[0].filtered == 0
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM newsletter_issues").fetchone()[0] == 1
    entry = connection.execute("SELECT * FROM newsletter_entries").fetchone()
    assert entry["title"] == "Deep research agent launch"
    assert entry["tracked_url"] == "https://track/x"
    assert connection.execute("SELECT uid_validity FROM newsletter_issues").fetchone()[0] == "777"


def test_dry_run_classifies_existing_item_without_mutation(runner_parts):
    connection, repository, registry = runner_parts
    now = datetime.now(timezone.utc)
    repository.upsert_source(source_id="one", provider="fake", display_name="one", source_type="test", tier=0.8, now=now)
    repository.upsert_item(
        ResearchItemDraft(title="Paper 0", url="https://example.com/0"),
        source=SourceEvidence("one"),
        discovered_at=now,
    )
    registry.register("fake", FakeProvider(ProviderResult(items=_items(2), requests=1)))
    summary = SourceRunner(providers=registry, repository=repository).collect(
        SourceCatalog(1, (_source(),)), active_topic_ids=(), started_at=now, dry_run=True,
    )
    assert (summary.sources[0].new, summary.sources[0].unchanged) == (1, 1)
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 1


def test_provider_failure_is_accounted_and_next_source_runs(runner_parts):
    connection, repository, registry = runner_parts
    registry.register("bad", FakeProvider(error=RuntimeError("boom")))
    registry.register("good", FakeProvider(ProviderResult(items=_items(1), requests=1)))
    catalog = SourceCatalog(1, (_source("bad-source", "bad"), _source("good-source", "good")))
    summary = SourceRunner(providers=registry, repository=repository).collect(
        catalog, active_topic_ids=(), started_at=datetime.now(timezone.utc),
    )
    assert [s.status for s in summary.sources] == ["failed", "success"]
    assert summary.sources[0].error_message == "boom"
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 1


def test_provider_request_budget_violation_fails_without_items(runner_parts):
    connection, repository, registry = runner_parts
    registry.register("fake", FakeProvider(ProviderResult(items=_items(1), requests=5)))
    summary = SourceRunner(providers=registry, repository=repository).collect(
        SourceCatalog(1, (_source(max_requests=2),)),
        active_topic_ids=(), started_at=datetime.now(timezone.utc),
    )
    assert summary.sources[0].status == "failed"
    assert summary.sources[0].requests == 5
    assert "exceeded request budget" in summary.sources[0].error_message
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 0


def test_global_request_budget_stops_later_sources(runner_parts):
    _, repository, registry = runner_parts
    registry.register("fake", FakeProvider(ProviderResult(requests=2)))
    catalog = SourceCatalog(1, (_source("one"), _source("two")))
    summary = SourceRunner(
        providers=registry, repository=repository,
        budget=CollectionBudget(max_requests=2, max_new_items=10),
    ).collect(catalog, active_topic_ids=(), started_at=datetime.now(timezone.utc))
    assert [s.source_id for s in summary.sources] == ["one"]


def test_only_source_rejects_disabled_or_unknown(runner_parts):
    _, repository, registry = runner_parts
    registry.register("fake", FakeProvider())
    runner = SourceRunner(providers=registry, repository=repository)
    catalog = SourceCatalog(1, (_source(enabled=False),))
    with pytest.raises(ValueError, match="disabled"):
        runner.collect(catalog, active_topic_ids=(), started_at=datetime.now(timezone.utc), only_source_id="one")
    with pytest.raises(ValueError, match="Unknown catalog source"):
        runner.collect(catalog, active_topic_ids=(), started_at=datetime.now(timezone.utc), only_source_id="missing")


def test_retry_preserves_request_accounting_and_reduces_remaining_budget(runner_parts):
    connection, repository, registry = runner_parts
    provider = SequenceProvider([
        ProviderError("temporary", code="timeout", requests=1, retryable=True),
        ProviderResult(items=_items(1), requests=1),
    ])
    registry.register("fake", provider)
    sleeps = []
    summary = SourceRunner(
        providers=registry, repository=repository, sleeper=sleeps.append,
    ).collect(
        SourceCatalog(1, (_source(max_requests=3),)),
        active_topic_ids=("research-agent",),
        topic_match_terms={"research-agent": ("agent trajectories",)},
        started_at=datetime.now(timezone.utc),
    )
    assert summary.sources[0].requests == 2
    assert [context.remaining_requests for context in provider.contexts] == [3, 2]
    assert all(context.topic_match_terms == {"research-agent": ("agent trajectories",)} for context in provider.contexts)
    assert sleeps == [5.0]
    assert connection.execute("SELECT requests FROM source_runs").fetchone()[0] == 2


def test_single_topic_budget_filters_excess_items(runner_parts):
    from research_copilot.library import TopicMatch
    from research_copilot.sources import SourceBudget
    connection, repository, registry = runner_parts
    topic_items = tuple(
        ProviderItem(
            ResearchItemDraft(title=f"T{i}", url=f"https://example.com/t{i}"),
            topics=(TopicMatch("research-agent", 0.8),),
        )
        for i in range(3)
    )
    registry.register("fake", FakeProvider(ProviderResult(items=topic_items, requests=1)))
    source = SourceDefinition(
        id="one", provider="fake", display_name="one", source_type="test",
        enabled=True, tier=0.8, topics=("*",),
        budget=SourceBudget(max_requests=3, max_items=5, max_items_per_topic=1),
    )
    summary = SourceRunner(providers=registry, repository=repository).collect(
        SourceCatalog(1, (source,)), active_topic_ids=("research-agent",),
        started_at=datetime.now(timezone.utc),
    )
    assert (summary.sources[0].new, summary.sources[0].filtered) == (1, 2)
    assert summary.sources[0].fetched == 3
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 1


def test_runner_passes_only_scoped_topic_queries(runner_parts):
    _, repository, registry = runner_parts
    provider = FakeProvider()
    registry.register("fake", provider)
    source = SourceDefinition(
        id="one", provider="fake", display_name="one", source_type="test",
        enabled=True, tier=0.8, topics=("research-agent",),
    )
    SourceRunner(providers=registry, repository=repository).collect(
        SourceCatalog(1, (source,)),
        active_topic_ids=("research-agent", "inference"),
        topic_queries={
            "research-agent": ("deep research agent",),
            "inference": ("speculative decoding",),
        },
        started_at=datetime.now(timezone.utc), dry_run=True,
    )
    assert provider.contexts[0].active_topic_ids == ("research-agent",)
    assert provider.contexts[0].topic_queries == {"research-agent": ("deep research agent",)}


def test_runner_centrally_matches_and_excludes_untagged_items(runner_parts):
    connection, repository, registry = runner_parts
    items = (
        ProviderItem(ResearchItemDraft(title="Deep research agent benchmark", url="https://example.com/good")),
        ProviderItem(ResearchItemDraft(title="Deep research agent for pure RAG", url="https://example.com/excluded")),
        ProviderItem(ResearchItemDraft(title="Unrelated cooking post", url="https://example.com/nope")),
    )
    registry.register("fake", FakeProvider(ProviderResult(items=items, requests=1)))
    summary = SourceRunner(providers=registry, repository=repository).collect(
        SourceCatalog(1, (_source(max_items=10),)),
        active_topic_ids=("research-agent",),
        topic_queries={"research-agent": ("deep research agent", "benchmark")},
        topic_excludes={"research-agent": ("pure RAG",)},
        started_at=datetime.now(timezone.utc),
    )
    assert (summary.sources[0].new, summary.sources[0].filtered) == (1, 2)
    topic = connection.execute("SELECT * FROM item_topics").fetchone()
    assert topic["topic_id"] == "research-agent"
    assert topic["confidence"] == 0.6


def test_dry_run_previews_topic_rejection_reason(runner_parts):
    _, repository, registry = runner_parts
    item = ProviderItem(ResearchItemDraft(
        title="Warehouse inventory forecasting", url="https://example.com/nope",
    ))
    registry.register("fake", FakeProvider(ProviderResult(items=(item,), requests=1)))
    summary = SourceRunner(providers=registry, repository=repository).collect(
        SourceCatalog(1, (_source(),)), active_topic_ids=("research-agent",),
        topic_match_terms={"research-agent": ("research agent",)},
        started_at=datetime.now(timezone.utc), dry_run=True,
    )
    preview = summary.sources[0].previews[0]
    assert preview.disposition == "filtered"
    assert preview.reason == "no_topic_match"
    assert summary.sources[0].filtered == 1
    assert summary.sources[0].metrics["topic_rejected_count"] == 1


def test_runner_rejects_provider_topics_outside_active_scope(runner_parts):
    from research_copilot.library import TopicMatch
    connection, repository, registry = runner_parts
    item = ProviderItem(
        ResearchItemDraft(title="Paper", url="https://example.com/p"),
        topics=(TopicMatch("inactive-topic", 0.9),),
    )
    registry.register("fake", FakeProvider(ProviderResult(items=(item,), requests=1)))
    summary = SourceRunner(providers=registry, repository=repository).collect(
        SourceCatalog(1, (_source(),)),
        active_topic_ids=("research-agent",),
        topic_queries={"research-agent": ("paper",)},
        started_at=datetime.now(timezone.utc),
    )
    assert (summary.sources[0].new, summary.sources[0].filtered) == (0, 1)
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 0


def test_runner_content_validates_query_assigned_topics(runner_parts):
    from research_copilot.library import TopicMatch

    connection, repository, registry = runner_parts
    items = (
        ProviderItem(
            ResearchItemDraft(
                title="Long horizon agents plan across tool-use trajectories",
                url="https://example.com/relevant",
            ),
            topics=(TopicMatch("long-horizon-agent", 0.5, ("agent planning",)),),
        ),
        ProviderItem(
            ResearchItemDraft(
                title="Long horizon forecasting for warehouse inventory",
                url="https://example.com/irrelevant",
            ),
            topics=(TopicMatch("long-horizon-agent", 0.5, ("agent planning",)),),
        ),
    )
    registry.register("fake", FakeProvider(ProviderResult(items=items, requests=1)))
    summary = SourceRunner(providers=registry, repository=repository).collect(
        SourceCatalog(1, (_source(max_items=10),)),
        active_topic_ids=("long-horizon-agent",),
        topic_match_terms={
            "long-horizon-agent": ("long-horizon agent", "tool use trajectory"),
        },
        started_at=datetime.now(timezone.utc),
    )
    assert (summary.sources[0].new, summary.sources[0].filtered) == (1, 1)
    topic = connection.execute("SELECT * FROM item_topics").fetchone()
    assert topic["topic_id"] == "long-horizon-agent"
    assert topic["confidence"] == 0.6


def test_runner_applies_excludes_to_query_assigned_topics(runner_parts):
    from research_copilot.library import TopicMatch

    connection, repository, registry = runner_parts
    item = ProviderItem(
        ResearchItemDraft(
            title="On-policy distillation for image generation compression",
            url="https://example.com/image-opd",
        ),
        topics=(TopicMatch("opd", 0.5, ("on-policy distillation",)),),
    )
    registry.register("fake", FakeProvider(ProviderResult(items=(item,), requests=1)))
    summary = SourceRunner(providers=registry, repository=repository).collect(
        SourceCatalog(1, (_source(max_items=10),)),
        active_topic_ids=("opd",),
        topic_match_terms={"opd": ("on-policy distillation",)},
        topic_excludes={"opd": ("image generation compression",)},
        started_at=datetime.now(timezone.utc),
    )
    assert (summary.sources[0].new, summary.sources[0].filtered) == (0, 1)
    assert connection.execute("SELECT count(*) FROM research_items").fetchone()[0] == 0


def test_runner_persists_provider_cursor_and_passes_it_to_next_run(runner_parts):
    connection, repository, registry = runner_parts
    provider = SequenceProvider([
        ProviderResult(requests=1, state_updates={"etag": '"v1"'}),
        ProviderResult(requests=1),
    ])
    registry.register("fake", provider)
    runner = SourceRunner(providers=registry, repository=repository)
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    runner.collect(SourceCatalog(1, (_source(),)), active_topic_ids=(), started_at=now)
    runner.collect(SourceCatalog(1, (_source(),)), active_topic_ids=(), started_at=now)
    assert provider.contexts[0].source_state == {}
    assert provider.contexts[1].source_state == {"etag": '"v1"'}
    assert repository.get_source_runtime_state("one")["provider_state"] == {"etag": '"v1"'}


def test_failure_cooldown_skips_persisted_runs_but_dry_run_bypasses(runner_parts):
    connection, repository, registry = runner_parts
    provider = FakeProvider(error=RuntimeError("offline"))
    registry.register("fake", provider)
    runner = SourceRunner(
        providers=registry, repository=repository,
        cooldown_policy=FailureCooldownPolicy(threshold=1, base_seconds=3600, max_seconds=7200),
    )
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    first = runner.collect(
        SourceCatalog(1, (_source(),)), active_topic_ids=(), started_at=now,
    )
    assert first.sources[0].status == "failed"
    assert first.sources[0].cooldown_until == "2026-08-08T01:00:00+00:00"

    skipped = runner.collect(
        SourceCatalog(1, (_source(),)), active_topic_ids=(),
        started_at=datetime(2026, 8, 8, 0, 30, tzinfo=timezone.utc),
    )
    assert skipped.sources[0].status == "cooldown"
    assert len(provider.contexts) == 1
    assert connection.execute("SELECT count(*) FROM source_runs").fetchone()[0] == 1

    diagnostic = runner.collect(
        SourceCatalog(1, (_source(),)), active_topic_ids=(),
        started_at=datetime(2026, 8, 8, 0, 30, tzinfo=timezone.utc), dry_run=True,
    )
    assert diagnostic.sources[0].status == "failed"
    assert len(provider.contexts) == 2
    assert provider.contexts[-1].source_state == {}
    assert repository.get_source_runtime_state("one")["consecutive_failures"] == 1
