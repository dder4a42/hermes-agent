"""RSS and Atom source provider."""

from __future__ import annotations

import re
import inspect
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Callable, Mapping
from urllib.parse import urljoin, urlparse

from research_copilot.library import ResearchItemDraft
from research_copilot.library.identity import normalize_url
from research_copilot.net import HttpResponse, fetch as _net_fetch
from research_copilot.sources.models import SourceDefinition

from .base import FetchContext, ProviderError, ProviderItem, ProviderResult

HttpFetcher = Callable[..., bytes | HttpResponse]


def _default_fetch(url: str, timeout: int, conditional_headers: Mapping[str, str]) -> HttpResponse:
    headers = {
        "User-Agent": "Hermes-Research-Copilot/1.0",
        "Accept": "application/atom+xml,application/rss+xml,application/xml,text/xml",
        **conditional_headers,
    }
    return _net_fetch(
        url, timeout=timeout,
        headers=headers, return_response=True,
    )


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
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            return raw[:64]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _element_text(element: ET.Element) -> str:
    return "".join(element.itertext()).strip()


def _values(entry: ET.Element, *names: str) -> list[str]:
    wanted = {name.casefold() for name in names}
    return [
        _element_text(child)
        for child in entry
        if _local_name(child.tag) in wanted and _element_text(child)
    ]


def _first(entry: ET.Element, *names: str) -> str:
    values = _values(entry, *names)
    return values[0] if values else ""


def _entry_link(entry: ET.Element, *, base_url: str, atom_mode: bool) -> str:
    links = [child for child in entry if _local_name(child.tag) in {"link", "origlink"}]
    candidates: list[str] = []
    if atom_mode:
        for element in links:
            rel = element.attrib.get("rel", "alternate").casefold()
            media_type = element.attrib.get("type", "").casefold()
            if rel in {"", "alternate"} and media_type not in {"application/atom+xml", "application/rss+xml"}:
                candidates.append(element.attrib.get("href", "") or _element_text(element))
    else:
        # FeedBurner origLink is normally canonical and should beat its tracking link.
        candidates.extend(_element_text(element) for element in links if _local_name(element.tag) == "origlink")
        candidates.extend(
            element.attrib.get("href", "") or _element_text(element)
            for element in links if _local_name(element.tag) == "link"
        )
    for child in entry:
        local = _local_name(child.tag)
        if local == "id" and atom_mode:
            candidates.append(_element_text(child))
        elif (
            local == "guid"
            and child.attrib.get("isPermaLink", "true").casefold() != "false"
        ):
            candidates.append(_element_text(child))
    candidates.extend(
        value for key, value in entry.attrib.items()
        if _local_name(key) == "about"
    )
    for raw in candidates:
        absolute = urljoin(base_url, raw.strip())
        if urlparse(absolute).scheme in {"http", "https"}:
            return normalize_url(absolute) or absolute
    return ""


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
        conditional_headers = {}
        if context.source_state.get("etag"):
            conditional_headers["If-None-Match"] = str(context.source_state["etag"])
        if context.source_state.get("last_modified"):
            conditional_headers["If-Modified-Since"] = str(context.source_state["last_modified"])
        try:
            try:
                parameters = tuple(inspect.signature(self.fetcher).parameters.values())
                accepts_headers = any(p.kind == p.VAR_POSITIONAL for p in parameters) or len(parameters) >= 3
            except (TypeError, ValueError):
                accepts_headers = False
            response = (
                self.fetcher(feed_url, timeout, conditional_headers)
                if accepts_headers else self.fetcher(feed_url, timeout)
            )
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
        state_updates = {}
        if isinstance(response, HttpResponse):
            if response.status == 304:
                return ProviderResult(requests=1)
            payload = response.body
            lowered = {key.lower(): value for key, value in response.headers.items()}
            if lowered.get("etag"):
                state_updates["etag"] = lowered["etag"]
            if lowered.get("last-modified"):
                state_updates["last_modified"] = lowered["last-modified"]
        else:
            payload = response
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ProviderError(
                f"RSS parse failed: {exc}", code="parse_error", requests=1,
            ) from exc
        items, metrics = self._parse(
            root, limit=context.remaining_items, base_url=feed_url,
        )
        return ProviderResult(
            items=tuple(items), requests=1, state_updates=state_updates,
            metrics=metrics,
        )

    def _parse(
        self, root: ET.Element, *, limit: int, base_url: str = "",
    ) -> tuple[list[ProviderItem], dict[str, int | str]]:
        entries = [element for element in root.iter() if _local_name(element.tag) == "entry"]
        mode = "atom"
        if not entries:
            entries = [element for element in root.iter() if _local_name(element.tag) == "item"]
            mode = "rss"
        output: list[ProviderItem] = []
        seen: set[str] = set()
        missing_title = missing_link = duplicate_link = full_content = 0
        for entry in entries:
            if len(output) >= limit:
                break
            if mode == "atom":
                title = _first(entry, "title")
                summary_values = _values(entry, "summary", "content", "encoded", "description")
                published = _first(entry, "published", "updated", "date")
            else:
                title = _first(entry, "title")
                summary_values = _values(entry, "description", "encoded", "content", "summary")
                published = _first(entry, "pubdate", "published", "date", "updated")
            title = _clean_html(title)
            if not title:
                missing_title += 1
                continue
            link = _entry_link(entry, base_url=base_url, atom_mode=mode == "atom")
            if not link:
                missing_link += 1
                continue
            if link in seen:
                duplicate_link += 1
                continue
            seen.add(link)
            cleaned_summaries = [_clean_html(value) for value in summary_values]
            summary = max(cleaned_summaries, key=len, default="")
            if len(cleaned_summaries) > 1 and summary != cleaned_summaries[0]:
                full_content += 1
            author_values = _values(entry, "creator")
            for author in [child for child in entry if _local_name(child.tag) == "author"]:
                name = _first(author, "name") or _element_text(author)
                if name:
                    author_values.append(name)
            categories = tuple(dict.fromkeys(
                child.attrib.get("term", "") or _element_text(child)
                for child in entry if _local_name(child.tag) == "category"
                if child.attrib.get("term", "") or _element_text(child)
            ))
            output.append(
                ProviderItem(
                    item=ResearchItemDraft(
                        title=title,
                        item_type="research_signal",
                        summary=summary[:4000],
                        url=link,
                        authors=tuple(dict.fromkeys(_clean_html(value) for value in author_values if _clean_html(value))),
                        published_at=_date(published),
                        metadata={"feed_categories": categories} if categories else {},
                    )
                )
            )
        return output, {
            "feed_format": mode,
            "parsed_entry_count": len(entries),
            "emitted_item_count": len(output),
            "missing_title_count": missing_title,
            "missing_link_count": missing_link,
            "duplicate_link_count": duplicate_link,
            "full_content_count": full_content,
        }
