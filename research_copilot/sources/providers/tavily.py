"""Tavily topic-search provider for catalog-constrained domains."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urlsplit

from research_copilot.library import ResearchItemDraft, TopicMatch
from research_copilot.library.identity import normalize_url
from research_copilot.net import fetch as _net_fetch
from research_copilot.sources.models import SourceDefinition
from research_copilot.sources.query_plan import build_query_plan
from research_copilot.sources.topic_matching import match_topic_content

from .base import FetchContext, ProviderError, ProviderItem, ProviderResult, merge_provider_item_topics

HttpFetcher = Callable[[str, bytes, int, dict[str, str]], bytes]


def _default_fetch(url: str, payload: bytes, timeout: int, headers: dict[str, str]) -> bytes:
    return _net_fetch(url, timeout=timeout, headers=headers, data=payload, method="POST")


class TavilyProvider:
    API_URL = "https://api.tavily.com/search"

    def __init__(self, *, api_key: str, fetcher: HttpFetcher = _default_fetch) -> None:
        self.api_key = api_key
        self.fetcher = fetcher

    def fetch(self, source: SourceDefinition, context: FetchContext) -> ProviderResult:
        if not self.api_key:
            raise ProviderError("Tavily API key is not configured", code="missing_credential")
        timeout = int(source.options.get("timeout_seconds", 20))
        search_depth = str(source.options.get("search_depth", "advanced"))
        require_title_match = bool(source.options.get("require_title_match", False))
        domains = source.options.get("domains", ())
        blocked_hosts = source.options.get("blocked_hosts", ())
        if not isinstance(domains, (list, tuple)) or not all(isinstance(d, str) for d in domains):
            raise ProviderError("Tavily options.domains must be a list of strings", code="invalid_config")
        if not isinstance(blocked_hosts, (list, tuple)) or not all(
            isinstance(host, str) for host in blocked_hosts
        ):
            raise ProviderError(
                "Tavily options.blocked_hosts must be a list of strings",
                code="invalid_config",
            )
        queries = build_query_plan(context, source_id=source.id)
        items: list[ProviderItem] = []
        requests = 0
        seen: dict[str, int] = {}
        title_rejected = 0
        for planned in queries:
            query = planned.query
            if requests >= context.remaining_requests or len(items) >= context.remaining_items:
                break
            body = {
                "api_key": self.api_key,
                "query": query,
                "search_depth": search_depth,
                "max_results": min(5, context.remaining_items - len(items)),
            }
            if domains:
                body["include_domains"] = list(domains)
            payload = json.dumps(body).encode("utf-8")
            try:
                raw = self.fetcher(
                    self.API_URL, payload, timeout,
                    {"Content-Type": "application/json", "Accept": "application/json"},
                )
                requests += 1
            except urllib.error.HTTPError as exc:
                requests += 1
                if items:
                    return ProviderResult(
                        items=tuple(items), requests=requests,
                        rate_limited=exc.code == 429,
                        error_code=f"http_{exc.code}",
                        error_message=f"Tavily HTTP {exc.code}",
                    )
                raise ProviderError(
                    f"Tavily HTTP {exc.code}", code=f"http_{exc.code}",
                    requests=requests, retryable=exc.code == 429 or exc.code >= 500,
                    rate_limited=exc.code == 429,
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                requests += 1
                raise ProviderError(
                    f"Tavily fetch failed: {exc}", code="network_error",
                    requests=requests, retryable=True,
                ) from exc
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ProviderError(
                    f"Tavily returned invalid JSON: {exc}",
                    code="parse_error", requests=requests,
                ) from exc
            for rank, result in enumerate(data.get("results", []), start=1):
                title = str(result.get("title") or "").strip()
                url = str(result.get("url") or "").strip()
                if not title or not url:
                    continue
                hostname = (urlsplit(url).hostname or "").casefold()
                if any(
                    hostname == blocked.casefold()
                    or hostname.endswith("." + blocked.casefold())
                    for blocked in blocked_hosts
                ):
                    title_rejected += 1
                    continue
                if require_title_match:
                    title_matches = any(
                        match_topic_content(
                            title,
                            include_terms=context.topic_match_terms.get(topic_id, ()),
                            exclude_terms=context.topic_excludes.get(topic_id, ()),
                        ).accepted
                        for topic_id in planned.topic_ids
                    )
                    if not title_matches:
                        title_rejected += 1
                        continue
                identity = normalize_url(url) or url
                planned_topics = tuple(
                    TopicMatch(topic_id, 0.5, (query,))
                    for topic_id in planned.topic_ids
                )
                if identity in seen:
                    index = seen[identity]
                    items[index] = merge_provider_item_topics(items[index], planned_topics)
                    continue
                seen[identity] = len(items)
                items.append(ProviderItem(
                    item=ResearchItemDraft(
                        title=title,
                        summary=str(result.get("content") or "")[:4000],
                        url=url,
                    ),
                    topics=planned_topics,
                    query=query,
                    rank=rank,
                    metadata={"score": result.get("score")},
                ))
                if len(items) >= context.remaining_items:
                    break
        return ProviderResult(
            items=tuple(items), requests=requests, filtered=title_rejected,
            metrics={"title_rejected_count": title_rejected},
        )
