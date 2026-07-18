"""Tavily topic-search provider for catalog-constrained domains."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from research_copilot.library import ResearchItemDraft, TopicMatch
from research_copilot.sources.models import SourceDefinition

from .base import FetchContext, ProviderError, ProviderItem, ProviderResult

HttpFetcher = Callable[[str, bytes, int, dict[str, str]], bytes]


def _default_fetch(url: str, payload: bytes, timeout: int, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


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
        domains = source.options.get("domains", ())
        if not isinstance(domains, (list, tuple)) or not all(isinstance(d, str) for d in domains):
            raise ProviderError("Tavily options.domains must be a list of strings", code="invalid_config")
        queries = [
            (topic_id, query.strip())
            for topic_id in context.active_topic_ids
            for query in context.topic_queries.get(topic_id, ())
            if query.strip()
        ]
        items: list[ProviderItem] = []
        requests = 0
        seen: set[str] = set()
        for topic_id, query in queries:
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
                if not title or not url or url in seen:
                    continue
                seen.add(url)
                items.append(ProviderItem(
                    item=ResearchItemDraft(
                        title=title,
                        summary=str(result.get("content") or "")[:4000],
                        url=url,
                    ),
                    topics=(TopicMatch(topic_id, 0.5, (query,)),),
                    query=query,
                    rank=rank,
                    metadata={"score": result.get("score")},
                ))
                if len(items) >= context.remaining_items:
                    break
        return ProviderResult(items=tuple(items), requests=requests)
