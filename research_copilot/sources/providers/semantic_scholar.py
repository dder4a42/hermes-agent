"""Semantic Scholar Graph API provider."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from research_copilot.library import ResearchItemDraft, TopicMatch
from research_copilot.net import fetch as _net_fetch
from research_copilot.sources.models import SourceDefinition
from research_copilot.sources.query_plan import build_query_plan

from .base import FetchContext, ProviderError, ProviderItem, ProviderResult, merge_provider_item_topics

HttpFetcher = Callable[[str, int, dict[str, str]], bytes]


def _default_fetch(url: str, timeout: int, headers: dict[str, str]) -> bytes:
    return _net_fetch(url, timeout=timeout, headers=headers)


class SemanticScholarProvider:
    API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

    def __init__(self, *, api_key: str = "", fetcher: HttpFetcher = _default_fetch) -> None:
        self.api_key = api_key
        self.fetcher = fetcher

    def fetch(self, source: SourceDefinition, context: FetchContext) -> ProviderResult:
        timeout = int(source.options.get("timeout_seconds", 15))
        limit = min(int(source.options.get("limit_per_query", 5)), context.remaining_items)
        queries = build_query_plan(context, source_id=source.id)
        if not queries:
            return ProviderResult()
        headers = {"User-Agent": "Hermes-Research-Copilot/1.0", "Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        items: list[ProviderItem] = []
        requests = 0
        seen: dict[str, int] = {}
        for planned in queries:
            query = planned.query
            if requests >= context.remaining_requests or len(items) >= context.remaining_items:
                break
            params = urllib.parse.urlencode({
                "query": query,
                "limit": limit,
                "fields": "paperId,title,url,abstract,authors,year,externalIds,publicationDate",
            })
            url = f"{self.API_URL}?{params}"
            try:
                payload = self.fetcher(url, timeout, headers)
                requests += 1
            except urllib.error.HTTPError as exc:
                requests += 1
                code = f"http_{exc.code}"
                if items:
                    return ProviderResult(
                        items=tuple(items), requests=requests,
                        rate_limited=exc.code == 429, error_code=code,
                        error_message=f"Semantic Scholar HTTP {exc.code}",
                    )
                raise ProviderError(
                    f"Semantic Scholar HTTP {exc.code}", code=code,
                    requests=requests, retryable=exc.code == 429 or exc.code >= 500,
                    rate_limited=exc.code == 429,
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                requests += 1
                raise ProviderError(
                    f"Semantic Scholar fetch failed: {exc}", code="network_error",
                    requests=requests, retryable=True,
                ) from exc
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ProviderError(
                    f"Semantic Scholar returned invalid JSON: {exc}",
                    code="parse_error", requests=requests,
                ) from exc
            for paper in data.get("data", []):
                title = str(paper.get("title") or "").strip()
                paper_id = str(paper.get("paperId") or "").strip()
                if not title:
                    continue
                external_ids = paper.get("externalIds") or {}
                arxiv_id = str(external_ids.get("ArXiv") or "")
                doi = str(external_ids.get("DOI") or "")
                url_value = str(paper.get("url") or "")
                if not url_value and arxiv_id:
                    url_value = f"https://arxiv.org/abs/{arxiv_id}"
                identity = paper_id or arxiv_id or doi or url_value or title.casefold()
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
                        item_type="paper",
                        summary=str(paper.get("abstract") or "")[:4000],
                        url=url_value,
                        authors=tuple(
                            str(author.get("name") or "").strip()
                            for author in paper.get("authors") or []
                            if str(author.get("name") or "").strip()
                        ),
                        published_at=str(paper.get("publicationDate") or paper.get("year") or "") or None,
                        doi=doi,
                        arxiv_id=arxiv_id,
                        semantic_scholar_id=paper_id,
                    ),
                    topics=planned_topics,
                    query=query,
                ))
                if len(items) >= context.remaining_items:
                    break
        return ProviderResult(items=tuple(items), requests=requests)
