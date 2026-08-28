"""Hugging Face Daily Papers API provider."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from research_copilot.library import ResearchItemDraft
from research_copilot.sources.models import SourceDefinition

from .base import FetchContext, ProviderError, ProviderItem, ProviderResult

HttpFetcher = Callable[[str, int], bytes]


def _default_fetch(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Hermes-Research-Copilot/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class HuggingFaceDailyProvider:
    DEFAULT_ENDPOINT = "https://huggingface.co/api/daily_papers"

    def __init__(self, *, fetcher: HttpFetcher = _default_fetch) -> None:
        self.fetcher = fetcher

    def fetch(self, source: SourceDefinition, context: FetchContext) -> ProviderResult:
        if context.remaining_requests < 1:
            raise ProviderError("Hugging Face request budget is exhausted", code="budget_exhausted")
        endpoint = str(source.options.get("endpoint") or self.DEFAULT_ENDPOINT)
        timeout = int(source.options.get("timeout_seconds", 20))
        try:
            payload = self.fetcher(endpoint, timeout)
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                f"Hugging Face HTTP {exc.code}", code=f"http_{exc.code}",
                requests=1, retryable=exc.code == 429 or exc.code >= 500,
                rate_limited=exc.code == 429,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(
                f"Hugging Face fetch failed: {exc}", code="network_error",
                requests=1, retryable=True,
            ) from exc
        try:
            rows = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(
                f"Hugging Face returned invalid JSON: {exc}",
                code="parse_error", requests=1,
            ) from exc
        if not isinstance(rows, list):
            raise ProviderError(
                "Hugging Face response must be a list", code="parse_error", requests=1,
            )
        items: list[ProviderItem] = []
        seen: set[str] = set()
        for row in rows:
            paper = row.get("paper") if isinstance(row, dict) and isinstance(row.get("paper"), dict) else row
            if not isinstance(paper, dict):
                continue
            title = str(paper.get("title") or "").strip()
            paper_id = str(paper.get("id") or paper.get("paperId") or "").strip()
            if not title or not paper_id or paper_id in seen:
                continue
            seen.add(paper_id)
            authors = paper.get("authors") or []
            items.append(ProviderItem(item=ResearchItemDraft(
                title=title,
                item_type="paper",
                summary=str(paper.get("summary") or paper.get("abstract") or "")[:4000],
                url=f"https://huggingface.co/papers/{paper_id}",
                authors=tuple(
                    str(author.get("name") if isinstance(author, dict) else author).strip()
                    for author in authors
                    if str(author.get("name") if isinstance(author, dict) else author).strip()
                ),
                published_at=str(paper.get("publishedAt") or paper.get("published_at") or "") or None,
                arxiv_id=paper_id,
            )))
            if len(items) >= context.remaining_items:
                break
        return ProviderResult(items=tuple(items), requests=1)
