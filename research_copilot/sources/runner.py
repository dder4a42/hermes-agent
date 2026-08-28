"""Budgeted orchestration between source providers and the Research Library."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time
from typing import Callable
from typing import Mapping

from research_copilot.library import (
    LibraryRepository,
    SourceEvidence,
    SourceRunCounts,
    TopicMatch,
)

from .catalog import SourceCatalog
from .models import CollectionBudget, SourceDefinition
from .providers.base import FetchContext, ProviderError, ProviderItem, ProviderResult
from .registry import ProviderRegistry


@dataclass(frozen=True)
class SourceSummary:
    source_id: str
    status: str
    requests: int = 0
    fetched: int = 0
    new: int = 0
    merged: int = 0
    unchanged: int = 0
    filtered: int = 0
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class CollectionSummary:
    sources: tuple[SourceSummary, ...]
    dry_run: bool

    @property
    def requests(self) -> int:
        return sum(source.requests for source in self.sources)

    @property
    def new(self) -> int:
        return sum(source.new for source in self.sources)


class SourceRunner:
    def __init__(
        self,
        *,
        providers: ProviderRegistry,
        repository: LibraryRepository,
        budget: CollectionBudget = CollectionBudget(),
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.providers = providers
        self.repository = repository
        self.budget = budget
        self.sleeper = sleeper

    def collect(
        self,
        catalog: SourceCatalog,
        *,
        active_topic_ids: tuple[str, ...],
        started_at: datetime,
        dry_run: bool = False,
        only_source_id: str | None = None,
        topic_queries: Mapping[str, tuple[str, ...]] | None = None,
        topic_excludes: Mapping[str, tuple[str, ...]] | None = None,
    ) -> CollectionSummary:
        summaries: list[SourceSummary] = []
        requests_used = 0
        new_items = 0
        for source in catalog.sources:
            if not source.enabled or (only_source_id and source.id != only_source_id):
                continue
            remaining_requests = self.budget.max_requests - requests_used
            remaining_items = self.budget.max_new_items - new_items
            if remaining_requests <= 0 or remaining_items <= 0:
                break
            summary = self._collect_source(
                source,
                active_topic_ids=active_topic_ids,
                started_at=started_at,
                remaining_requests=remaining_requests,
                remaining_items=remaining_items,
                dry_run=dry_run,
                topic_queries=topic_queries or {},
                topic_excludes=topic_excludes or {},
            )
            summaries.append(summary)
            requests_used += summary.requests
            new_items += summary.new
        if only_source_id and not any(s.source_id == only_source_id for s in summaries):
            try:
                source = catalog.by_id(only_source_id)
            except KeyError:
                raise ValueError(f"Unknown catalog source: {only_source_id}") from None
            if not source.enabled:
                raise ValueError(f"Source is disabled: {only_source_id}")
        return CollectionSummary(sources=tuple(summaries), dry_run=dry_run)

    def _collect_source(
        self,
        source: SourceDefinition,
        *,
        active_topic_ids: tuple[str, ...],
        started_at: datetime,
        remaining_requests: int,
        remaining_items: int,
        dry_run: bool,
        topic_queries: Mapping[str, tuple[str, ...]],
        topic_excludes: Mapping[str, tuple[str, ...]],
    ) -> SourceSummary:
        allowed_requests = min(source.budget.max_requests, remaining_requests)
        allowed_items = min(source.budget.max_items, remaining_items)
        scoped_topics = (
            active_topic_ids
            if "*" in source.topics
            else tuple(topic for topic in active_topic_ids if topic in source.topics)
        )
        context = FetchContext(
            started_at=started_at,
            active_topic_ids=scoped_topics,
            remaining_requests=allowed_requests,
            remaining_items=allowed_items,
            topic_queries={
                topic_id: tuple(topic_queries.get(topic_id, ()))
                for topic_id in scoped_topics
            },
            topic_excludes={
                topic_id: tuple(topic_excludes.get(topic_id, ()))
                for topic_id in scoped_topics
            },
        )
        run_id: str | None = None
        if not dry_run:
            self.repository.upsert_source(
                source_id=source.id,
                provider=source.provider,
                display_name=source.display_name,
                source_type=source.source_type,
                tier=source.tier,
                enabled=source.enabled,
                config=dict(source.options),
                now=started_at,
            )
            run_id = self.repository.start_source_run(
                source_id=source.id, started_at=started_at,
            )
        requests_spent = 0
        try:
            result = self._fetch_with_retry(source, context)
            requests_spent = result.requests
            upstream_fetched = len(result.items)
            if not dry_run and source.provider == "gmail_newsletter":
                self._stage_newsletters(source, result, created_at=started_at)
                # Gmail is a discovery channel. Entries must be URL-resolved,
                # enriched and topic-gated by NewsletterPromoter before they
                # become canonical Research Items.
                result = ProviderResult(
                    items=(), requests=result.requests, filtered=result.filtered,
                    rate_limited=result.rate_limited,
                    error_code=result.error_code, error_message=result.error_message,
                )
            result = self._match_topics(result, context)
            summary = self._persist_result(
                source, result, run_id=run_id, discovered_at=started_at,
                allowed_items=allowed_items, dry_run=dry_run,
                upstream_fetched=upstream_fetched,
            )
        except ProviderError as exc:
            requests_spent = exc.requests
            summary = SourceSummary(
                source_id=source.id,
                status="failed",
                requests=requests_spent,
                error_code=exc.code,
                error_message=str(exc),
            )
        except Exception as exc:
            summary = SourceSummary(
                source_id=source.id, status="failed",
                error_code="provider_error", error_message=str(exc),
            )
        if run_id is not None:
            self.repository.finish_source_run(
                run_id,
                status=summary.status,
                finished_at=started_at,
                counts=SourceRunCounts(
                    requests=summary.requests,
                    fetched=summary.fetched,
                    new=summary.new,
                    merged=summary.merged,
                    unchanged=summary.unchanged,
                    filtered=summary.filtered,
                ),
                error_code=summary.error_code,
                error_message=summary.error_message,
            )
        return summary

    def _stage_newsletters(
        self, source: SourceDefinition, result: ProviderResult, *, created_at: datetime,
    ) -> None:
        grouped: dict[str, list[ProviderItem]] = {}
        for item in result.items:
            body_hash = str(item.item.metadata.get("newsletter_body_hash") or "")
            if body_hash:
                grouped.setdefault(body_hash, []).append(item)
        for body_hash, items in grouped.items():
            first = items[0].item.metadata
            self.repository.record_newsletter_issue(
                source_id=source.id,
                mailbox=str(first.get("newsletter_label") or "ResearchFeeds"),
                uid=str(first.get("newsletter_uid") or body_hash),
                message_id=str(first.get("newsletter_message_id") or ""),
                sender=str(first.get("newsletter_sender") or ""),
                subject=str(first.get("newsletter_subject") or ""),
                received_at=str(first.get("newsletter_received_at") or "") or None,
                body_hash=body_hash,
                parser_id=str(first.get("newsletter_parser") or "unknown"),
                created_at=created_at,
                entries=tuple({
                    "section": item.item.metadata.get("newsletter_section", ""),
                    "title": item.item.title,
                    "excerpt": item.item.summary,
                    "tracked_url": item.item.metadata.get("newsletter_tracked_url", ""),
                    "canonical_url": item.item.url,
                    "content_type": item.item.item_type,
                    "classification_confidence": item.item.metadata.get("classification_confidence", .5),
                } for item in items),
            )

    def _fetch_with_retry(
        self,
        source: SourceDefinition,
        context: FetchContext,
    ) -> ProviderResult:
        provider = self.providers.get(source.provider)
        requests_spent = 0
        last_error: ProviderError | None = None
        for attempt in range(1, source.retry.max_attempts + 1):
            remaining = context.remaining_requests - requests_spent
            if remaining <= 0:
                break
            attempt_context = FetchContext(
                started_at=context.started_at,
                active_topic_ids=context.active_topic_ids,
                remaining_requests=remaining,
                remaining_items=context.remaining_items,
                topic_queries=context.topic_queries,
                topic_excludes=context.topic_excludes,
            )
            try:
                result = provider.fetch(source, attempt_context)
            except ProviderError as exc:
                requests_spent += exc.requests
                last_error = ProviderError(
                    str(exc), code=exc.code, requests=requests_spent,
                    retryable=exc.retryable, rate_limited=exc.rate_limited,
                )
                if not exc.retryable or attempt >= source.retry.max_attempts:
                    raise last_error
                if requests_spent >= context.remaining_requests:
                    break
                self.sleeper(source.retry.backoff_seconds * attempt)
                continue
            total_requests = requests_spent + result.requests
            if total_requests > context.remaining_requests:
                raise ProviderError(
                    f"Provider {source.provider} exceeded request budget "
                    f"({total_requests} > {context.remaining_requests})",
                    code="budget_exceeded", requests=total_requests,
                )
            return ProviderResult(
                items=result.items,
                requests=total_requests,
                filtered=result.filtered,
                rate_limited=result.rate_limited,
                error_code=result.error_code,
                error_message=result.error_message,
            )
        if last_error is not None:
            raise last_error
        raise ProviderError(
            f"Provider {source.provider} exhausted its request budget",
            code="budget_exhausted", requests=requests_spent,
        )

    @staticmethod
    def _match_topics(result: ProviderResult, context: FetchContext) -> ProviderResult:
        """Apply one topic policy to non-query providers before persistence."""
        if not context.active_topic_ids:
            return result
        matched_items: list[ProviderItem] = []
        rejected = 0
        active = set(context.active_topic_ids)
        for provider_item in result.items:
            if provider_item.topics:
                topics = tuple(t for t in provider_item.topics if t.topic_id in active)
                if not topics:
                    rejected += 1
                    continue
                matched_items.append(ProviderItem(
                    item=provider_item.item,
                    topics=topics,
                    query=provider_item.query,
                    rank=provider_item.rank,
                    metadata=provider_item.metadata,
                ))
                continue
            text = f"{provider_item.item.title} {provider_item.item.summary}".casefold()
            inferred = []
            for topic_id in context.active_topic_ids:
                excludes = tuple(term.casefold() for term in context.topic_excludes.get(topic_id, ()) if term)
                if any(term in text for term in excludes):
                    continue
                terms = tuple(term for term in context.topic_queries.get(topic_id, ()) if term)
                hits = tuple(term for term in terms if term.casefold() in text)
                if hits:
                    inferred.append(TopicMatch(
                        topic_id=topic_id,
                        confidence=min(1.0, 0.5 + 0.1 * (len(hits) - 1)),
                        matched_terms=hits,
                    ))
            if not inferred:
                rejected += 1
                continue
            matched_items.append(ProviderItem(
                item=provider_item.item,
                topics=tuple(inferred),
                query=provider_item.query,
                rank=provider_item.rank,
                metadata=provider_item.metadata,
            ))
        return ProviderResult(
            items=tuple(matched_items),
            requests=result.requests,
            filtered=result.filtered + rejected,
            rate_limited=result.rate_limited,
            error_code=result.error_code,
            error_message=result.error_message,
        )

    def _persist_result(
        self,
        source: SourceDefinition,
        result: ProviderResult,
        *,
        run_id: str | None,
        discovered_at: datetime,
        allowed_items: int,
        dry_run: bool,
        upstream_fetched: int,
    ) -> SourceSummary:
        selected, budget_filtered = self._select_items(
            result.items, source=source, allowed_items=allowed_items,
        )
        filtered = result.filtered + budget_filtered
        new = merged = unchanged = 0
        if dry_run:
            for provider_item, topics in selected:
                disposition = self.repository.classify_item(
                    provider_item.item, source_id=source.id, topics=topics,
                )
                if disposition == "new":
                    new += 1
                elif disposition == "merged":
                    merged += 1
                else:
                    unchanged += 1
        else:
            for provider_item, topics in selected:
                outcome = self.repository.upsert_item(
                    provider_item.item,
                    source=SourceEvidence(
                        source_id=source.id,
                        source_run_id=run_id,
                        query=provider_item.query,
                        rank=provider_item.rank,
                        metadata=provider_item.metadata,
                    ),
                    topics=topics,
                    discovered_at=discovered_at,
                )
                if outcome.disposition == "new":
                    new += 1
                elif outcome.disposition == "merged":
                    merged += 1
                else:
                    unchanged += 1
        status = "partial" if result.error_code or result.rate_limited else "success"
        return SourceSummary(
            source_id=source.id,
            status=status,
            requests=result.requests,
            fetched=upstream_fetched,
            new=new,
            merged=merged,
            unchanged=unchanged,
            filtered=filtered,
            error_code=result.error_code,
            error_message=result.error_message,
        )

    @staticmethod
    def _select_items(
        items: tuple[ProviderItem, ...],
        *,
        source: SourceDefinition,
        allowed_items: int,
    ) -> tuple[list[tuple[ProviderItem, tuple]], int]:
        selected: list[tuple[ProviderItem, tuple]] = []
        topic_counts: dict[str, int] = {}
        filtered = 0
        for item in items:
            if len(selected) >= allowed_items:
                filtered += 1
                continue
            admitted_topics = tuple(
                topic for topic in item.topics
                if topic_counts.get(topic.topic_id, 0) < source.budget.max_items_per_topic
            )
            if item.topics and not admitted_topics:
                filtered += 1
                continue
            selected.append((item, admitted_topics))
            for topic in admitted_topics:
                topic_counts[topic.topic_id] = topic_counts.get(topic.topic_id, 0) + 1
        return selected, filtered
