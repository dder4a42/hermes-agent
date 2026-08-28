"""Bounded HTML metadata extraction for canonical research URLs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

from research_copilot.library.identity import normalize_arxiv_id, normalize_doi

from .url_resolver import _system_resolve, validate_public_url


@dataclass(frozen=True)
class PageMetadata:
    canonical_url: str
    title: str = ""
    description: str = ""
    author: str = ""
    publisher: str = ""
    published_at: str | None = None
    page_type: str = ""
    identifiers: dict[str, str] | None = None
    extraction_method: str = "html_metadata"
    content_hash: str = ""


class _MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.in_title = False
        self.meta: dict[str, str] = {}
        self.canonical = ""
        self.json_ld: list[str] = []
        self.links: list[str] = []
        self.in_json_ld = False
        self._json_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = {str(k).lower(): str(v or "") for k, v in attrs}
        if tag == "title": self.in_title = True
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            if key and values.get("content"): self.meta[key] = values["content"].strip()
        if tag in {"a", "link"} and values.get("href"):
            self.links.append(values["href"])
        if tag == "link" and "canonical" in values.get("rel", "").lower(): self.canonical = values.get("href", "")
        if tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self.in_json_ld = True; self._json_parts = []

    def handle_data(self, data):
        if self.in_title: self.title_parts.append(data)
        if self.in_json_ld: self._json_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "title": self.in_title = False
        if tag == "script" and self.in_json_ld:
            self.json_ld.append("".join(self._json_parts)); self.in_json_ld = False


class PageMetadataExtractor:
    def __init__(self, *, session=None, dns_resolver=_system_resolve, timeout: float = 8.0, max_bytes: int = 1_500_000):
        self.session = session or requests.Session(); self.dns_resolver = dns_resolver
        self.timeout = timeout; self.max_bytes = max_bytes

    def fetch(self, url: str) -> PageMetadata:
        validate_public_url(url, self.dns_resolver)
        response = self.session.get(url, allow_redirects=False, stream=True, timeout=self.timeout, headers={"User-Agent": "HermesResearchLibrary/1.0"})
        try:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                raise ValueError(f"Unsupported page content type: {content_type}")
            chunks=[]; size=0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > self.max_bytes: raise ValueError("Page exceeds metadata size limit")
                chunks.append(chunk)
            raw=b"".join(chunks)
        finally:
            response.close()
        parser=_MetadataParser(); parser.feed(raw.decode(response.encoding or "utf-8", errors="replace"))
        structured={}

        def structured_rows(value):
            if isinstance(value, list):
                for item in value:
                    yield from structured_rows(item)
            elif isinstance(value, dict):
                if value.get("@type"):
                    yield value
                if "@graph" in value:
                    yield from structured_rows(value["@graph"])

        for value in parser.json_ld:
            try:
                decoded=json.loads(value)
                structured=next(iter(structured_rows(decoded)), structured)
            except json.JSONDecodeError: pass
        title=str(structured.get("headline") or parser.meta.get("og:title") or " ".join(parser.title_parts)).strip()
        description=str(structured.get("description") or parser.meta.get("og:description") or parser.meta.get("description") or "").strip()
        author=structured.get("author", "")
        if isinstance(author,dict): author=author.get("name","")
        canonical=urljoin(url, parser.canonical or parser.meta.get("og:url", "") or url)
        identifiers: dict[str, str] = {}
        identifier_candidates: list[str] = [
            canonical,
            parser.meta.get("citation_doi", ""),
            parser.meta.get("dc.identifier", ""),
            parser.meta.get("citation_arxiv_id", ""),
            parser.meta.get("citation_pdf_url", ""),
            str(structured.get("doi") or ""),
            str(structured.get("url") or ""),
            *(urljoin(url, link) for link in parser.links),
        ]
        structured_identifier = structured.get("identifier")
        if isinstance(structured_identifier, str):
            identifier_candidates.append(structured_identifier)
        elif isinstance(structured_identifier, dict):
            identifier_candidates.extend(str(value) for value in structured_identifier.values())
        elif isinstance(structured_identifier, list):
            for value in structured_identifier:
                if isinstance(value, str):
                    identifier_candidates.append(value)
                elif isinstance(value, dict):
                    identifier_candidates.extend(str(item) for item in value.values())
        same_as = structured.get("sameAs")
        if isinstance(same_as, str):
            identifier_candidates.append(same_as)
        elif isinstance(same_as, list):
            identifier_candidates.extend(str(value) for value in same_as)
        decoded_html = raw.decode(response.encoding or "utf-8", errors="replace")
        identifier_candidates.extend(re.findall(
            r"https?://(?:dx\.)?doi\.org/10\.\d{1,9}/[^\s\"'<>]+|"
            r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/[^\s\"'<>]+",
            decoded_html,
            flags=re.I,
        ))
        for candidate in identifier_candidates:
            if "doi" not in identifiers and (doi := normalize_doi(candidate)):
                identifiers["doi"] = doi
            if "arxiv" not in identifiers and (arxiv := normalize_arxiv_id(candidate)):
                identifiers["arxiv"] = arxiv
        return PageMetadata(canonical, title, description, str(author), str(structured.get("publisher", {}).get("name", "") if isinstance(structured.get("publisher"),dict) else structured.get("publisher", "")), structured.get("datePublished"), str(structured.get("@type") or ""), identifiers, content_hash=hashlib.sha256(raw).hexdigest())
