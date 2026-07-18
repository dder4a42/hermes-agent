"""GitHub Trending HTML provider."""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
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


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


class GitHubTrendingProvider:
    def __init__(self, *, fetcher: HttpFetcher = _default_fetch) -> None:
        self.fetcher = fetcher

    def fetch(self, source: SourceDefinition, context: FetchContext) -> ProviderResult:
        languages = source.options.get("languages", ["python"])
        if not isinstance(languages, (list, tuple)) or not all(isinstance(v, str) and v for v in languages):
            raise ProviderError("GitHub options.languages must be a non-empty list of strings", code="invalid_config")
        timeout = int(source.options.get("timeout_seconds", 20))
        since = str(source.options.get("since", "daily"))
        if since not in {"daily", "weekly", "monthly"}:
            raise ProviderError("GitHub options.since must be daily, weekly or monthly", code="invalid_config")
        items: list[ProviderItem] = []
        requests = 0
        seen: set[str] = set()
        for language in languages:
            if requests >= context.remaining_requests or len(items) >= context.remaining_items:
                break
            url = (
                "https://github.com/trending/"
                f"{urllib.parse.quote(language)}?since={urllib.parse.quote(since)}"
            )
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
                        error_message=f"GitHub HTTP {exc.code}",
                    )
                raise ProviderError(
                    f"GitHub HTTP {exc.code}", code=f"http_{exc.code}",
                    requests=requests, retryable=exc.code == 429 or exc.code >= 500,
                    rate_limited=exc.code == 429,
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                requests += 1
                raise ProviderError(
                    f"GitHub fetch failed: {exc}", code="network_error",
                    requests=requests, retryable=True,
                ) from exc
            html = payload.decode("utf-8", errors="replace")
            for block in re.findall(r'<article\b[^>]*class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>', html, re.I | re.S):
                path_match = re.search(r'<h2\b.*?<a\b[^>]*href="/([^"?#]+)"', block, re.I | re.S)
                if not path_match:
                    continue
                repo_path = path_match.group(1).strip("/")
                if repo_path.count("/") != 1 or repo_path in seen:
                    continue
                seen.add(repo_path)
                description_match = re.search(r'<p\b[^>]*>(.*?)</p>', block, re.I | re.S)
                description = _text(description_match.group(1)) if description_match else ""
                stars_match = re.search(r'([\d,]+)\s+stars?\s+(?:today|this week|this month)', _text(block), re.I)
                stars = int(stars_match.group(1).replace(",", "")) if stars_match else None
                summary = description
                if stars is not None:
                    summary = f"{description} Stars in period: {stars}".strip()
                owner, name = repo_path.split("/", 1)
                items.append(ProviderItem(
                    item=ResearchItemDraft(
                        title=name,
                        item_type="project",
                        summary=summary,
                        url=f"https://github.com/{repo_path}",
                        authors=(owner,),
                        metadata={"language": language, "stars_in_period": stars},
                    ),
                    metadata={"language": language, "stars_in_period": stars},
                ))
                if len(items) >= context.remaining_items:
                    break
        return ProviderResult(items=tuple(items), requests=requests)
