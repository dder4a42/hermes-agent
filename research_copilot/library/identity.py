"""Stable external-identifier and canonical-key normalization."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import ResearchItemDraft

_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def normalize_doi(value: str) -> str:
    doi = (value or "").strip().lower()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi.rstrip("/.,; ")


def normalize_arxiv_id(value: str) -> str:
    arxiv_id = (value or "").strip()
    match = re.search(r"(?:arxiv:|arxiv\.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5})(?:v\d+)?", arxiv_id, re.I)
    return match.group(1) if match else ""


def normalize_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
    ]
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(sorted(query)), ""))


def normalize_title(value: str) -> str:
    title = unicodedata.normalize("NFKC", value or "").casefold()
    title = re.sub(r"[^\w\s]", " ", title, flags=re.UNICODE)
    return re.sub(r"\s+", " ", title).strip()


def external_identifiers(draft: ResearchItemDraft) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    if doi := normalize_doi(draft.doi):
        identifiers["doi"] = doi
    arxiv = normalize_arxiv_id(draft.arxiv_id) or normalize_arxiv_id(draft.url)
    if arxiv:
        identifiers["arxiv"] = arxiv
    if s2 := draft.semantic_scholar_id.strip():
        identifiers["semantic_scholar"] = s2
    if url := normalize_url(draft.url):
        identifiers["url"] = url
    return identifiers


def canonical_key(draft: ResearchItemDraft) -> str:
    identifiers = external_identifiers(draft)
    for scheme in ("doi", "arxiv", "semantic_scholar", "url"):
        if value := identifiers.get(scheme):
            return f"{scheme}:{value}"

    title = normalize_title(draft.title)
    if not title:
        raise ValueError("A research item needs an identifier, URL, or title")
    first_author = normalize_title(draft.authors[0]) if draft.authors else ""
    year_match = re.search(r"\b(19|20)\d{2}\b", draft.published_at or "")
    year = year_match.group(0) if year_match else ""
    return f"title:{title}|author:{first_author}|year:{year}"
