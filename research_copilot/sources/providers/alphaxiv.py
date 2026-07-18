"""AlphaXiv discussion-signal provider."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from html import unescape
from typing import Callable

from research_copilot.library import ResearchItemDraft
from research_copilot.sources.models import SourceDefinition

from .base import FetchContext, ProviderError, ProviderItem, ProviderResult

HttpFetcher = Callable[[str, int], bytes]


def _default_fetch(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Hermes-Research-Copilot/1.0", "Accept": "text/html"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class AlphaXivProvider:
    def __init__(self, *, fetcher: HttpFetcher = _default_fetch) -> None:
        self.fetcher = fetcher

    def fetch(self, source: SourceDefinition, context: FetchContext) -> ProviderResult:
        if context.remaining_requests < 1:
            raise ProviderError("AlphaXiv request budget is exhausted", code="budget_exhausted")
        endpoint = str(source.options.get("endpoint") or "https://alphaxiv.org")
        timeout = int(source.options.get("timeout_seconds", 20))
        try:
            payload = self.fetcher(endpoint, timeout)
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                f"AlphaXiv HTTP {exc.code}", code=f"http_{exc.code}", requests=1,
                retryable=exc.code == 429 or exc.code >= 500,
                rate_limited=exc.code == 429,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(
                f"AlphaXiv fetch failed: {exc}", code="network_error",
                requests=1, retryable=True,
            ) from exc
        html = payload.decode("utf-8", errors="replace")
        items: list[ProviderItem] = []
        seen: set[str] = set()
        pattern = re.compile(
            r'<a\b[^>]*href="[^"]*/(?:abs|p)/(\d{4}\.\d{4,5})(?:v\d+)?[^"]*"[^>]*>(.*?)</a>',
            re.I | re.S,
        )
        for arxiv_id, title_html in pattern.findall(html):
            if arxiv_id in seen:
                continue
            seen.add(arxiv_id)
            title = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", title_html))).strip()
            if not title:
                continue
            items.append(ProviderItem(item=ResearchItemDraft(
                title=title,
                item_type="paper",
                url=f"https://arxiv.org/abs/{arxiv_id}",
                arxiv_id=arxiv_id,
                metadata={"discussion_url": f"https://alphaxiv.org/abs/{arxiv_id}"},
            )))
            if len(items) >= context.remaining_items:
                break
        return ProviderResult(items=tuple(items), requests=1)
