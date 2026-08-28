"""RSS and Atom source provider."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from typing import Callable

from research_copilot.library import ResearchItemDraft
from research_copilot.sources.models import SourceDefinition

from .base import FetchContext, ProviderError, ProviderItem, ProviderResult

HttpFetcher = Callable[[str, int], bytes]


def _default_fetch(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Hermes-Research-Copilot/1.0",
            "Accept": "application/atom+xml,application/rss+xml,application/xml,text/xml",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _clean_html(value: str | None) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", value or "", flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _date(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return raw[:64]


class RssProvider:
    def __init__(self, *, fetcher: HttpFetcher = _default_fetch) -> None:
        self.fetcher = fetcher

    def fetch(self, source: SourceDefinition, context: FetchContext) -> ProviderResult:
        if context.remaining_requests < 1:
            raise ProviderError("RSS request budget is exhausted", code="budget_exhausted")
        feed_url = str(source.options.get("feed_url") or "").strip()
        if not feed_url:
            raise ProviderError("RSS source requires options.feed_url", code="invalid_config")
        timeout = int(source.options.get("timeout_seconds", 20))
        try:
            payload = self.fetcher(feed_url, timeout)
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                f"RSS HTTP {exc.code}: {feed_url}",
                code=f"http_{exc.code}", requests=1,
                retryable=exc.code == 429 or exc.code >= 500,
                rate_limited=exc.code == 429,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(
                f"RSS fetch failed: {exc}", code="network_error",
                requests=1, retryable=True,
            ) from exc
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ProviderError(
                f"RSS parse failed: {exc}", code="parse_error", requests=1,
            ) from exc
        items = self._parse(root, limit=context.remaining_items)
        return ProviderResult(items=tuple(items), requests=1)

    def _parse(self, root: ET.Element, *, limit: int) -> list[ProviderItem]:
        atom = "{http://www.w3.org/2005/Atom}"
        entries = root.findall(f".//{atom}entry")
        mode = "atom"
        if not entries:
            entries = root.findall(".//item")
            mode = "rss"
        output: list[ProviderItem] = []
        seen: set[str] = set()
        for entry in entries:
            if len(output) >= limit:
                break
            if mode == "atom":
                title = entry.findtext(f"{atom}title", "")
                summary = entry.findtext(f"{atom}summary", "") or entry.findtext(f"{atom}content", "")
                published = entry.findtext(f"{atom}published", "") or entry.findtext(f"{atom}updated", "")
                link = ""
                for element in entry.findall(f"{atom}link"):
                    if element.attrib.get("rel", "alternate") in {"", "alternate"}:
                        link = element.attrib.get("href", "")
                        if link:
                            break
                link = link or entry.findtext(f"{atom}id", "")
            else:
                title = entry.findtext("title", "")
                summary = entry.findtext("description", "")
                published = entry.findtext("pubDate", "") or entry.findtext("published", "")
                link = entry.findtext("link", "")
            title = _clean_html(title)
            link = (link or "").strip()
            if not title or not link or link in seen:
                continue
            seen.add(link)
            output.append(
                ProviderItem(
                    item=ResearchItemDraft(
                        title=title,
                        item_type="research_signal",
                        summary=_clean_html(summary)[:2000],
                        url=link,
                        published_at=_date(published),
                    )
                )
            )
        return output
