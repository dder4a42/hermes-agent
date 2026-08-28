"""Budgeted arXiv API provider; catalog policy keeps it disabled by default."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable

from research_copilot.library import ResearchItemDraft, TopicMatch
from research_copilot.sources.models import SourceDefinition

from .base import FetchContext, ProviderError, ProviderItem, ProviderResult

HttpFetcher = Callable[[str, int], bytes]


def _default_fetch(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Hermes-Research-Copilot/1.0", "Accept": "application/atom+xml"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class ArxivProvider:
    API_URL = "https://export.arxiv.org/api/query"

    def __init__(self, *, fetcher: HttpFetcher = _default_fetch) -> None:
        self.fetcher = fetcher

    def fetch(self, source: SourceDefinition, context: FetchContext) -> ProviderResult:
        timeout = int(source.options.get("timeout_seconds", 20))
        per_query = min(int(source.options.get("max_results_per_query", 5)), context.remaining_items)
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
            expression = f'"{query}"' if " " in query else query
            params = urllib.parse.urlencode({
                "search_query": f"all:{expression}",
                "start": 0,
                "max_results": min(per_query, context.remaining_items - len(items)),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            })
            url = f"{self.API_URL}?{params}"
            try:
                payload = self.fetcher(url, timeout)
                requests += 1
            except urllib.error.HTTPError as exc:
                requests += 1
                if items:
                    return ProviderResult(
                        items=tuple(items), requests=requests,
                        rate_limited=exc.code == 429,
                        error_code=f"http_{exc.code}",
                        error_message=f"arXiv HTTP {exc.code}",
                    )
                raise ProviderError(
                    f"arXiv HTTP {exc.code}", code=f"http_{exc.code}",
                    requests=requests, retryable=exc.code == 429 or exc.code >= 500,
                    rate_limited=exc.code == 429,
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                requests += 1
                raise ProviderError(
                    f"arXiv fetch failed: {exc}", code="network_error",
                    requests=requests, retryable=True,
                ) from exc
            try:
                root = ET.fromstring(payload)
            except ET.ParseError as exc:
                raise ProviderError(
                    f"arXiv returned invalid Atom XML: {exc}",
                    code="parse_error", requests=requests,
                ) from exc
            atom = "{http://www.w3.org/2005/Atom}"
            for entry in root.findall(f"{atom}entry"):
                raw_id = (entry.findtext(f"{atom}id") or "").strip()
                arxiv_id = raw_id.rstrip("/").split("/")[-1].split("v")[0]
                title = " ".join((entry.findtext(f"{atom}title") or "").split())
                if not arxiv_id or not title or arxiv_id in seen:
                    continue
                seen.add(arxiv_id)
                authors = tuple(
                    (author.findtext(f"{atom}name") or "").strip()
                    for author in entry.findall(f"{atom}author")
                    if (author.findtext(f"{atom}name") or "").strip()
                )
                items.append(ProviderItem(
                    item=ResearchItemDraft(
                        title=title,
                        item_type="paper",
                        summary=" ".join((entry.findtext(f"{atom}summary") or "").split())[:4000],
                        url=f"https://arxiv.org/abs/{arxiv_id}",
                        authors=authors,
                        published_at=(entry.findtext(f"{atom}published") or "").strip() or None,
                        arxiv_id=arxiv_id,
                    ),
                    topics=(TopicMatch(topic_id, 0.5, (query,)),),
                    query=query,
                ))
                if len(items) >= context.remaining_items:
                    break
        return ProviderResult(items=tuple(items), requests=requests)
