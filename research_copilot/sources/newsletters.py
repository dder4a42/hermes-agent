"""Newsletter parsing independent from IMAP transport and Library persistence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse


@dataclass(frozen=True)
class NewsletterEntry:
    title: str
    url: str
    excerpt: str = ""
    section: str = ""
    content_type: str = "unknown"
    confidence: float = 0.5
    filter_reason: str = ""
    tracked_url: str = ""


@dataclass(frozen=True)
class ParsedNewsletter:
    parser_id: str
    subject: str
    entries: tuple[NewsletterEntry, ...]
    body_hash: str


def canonicalize_url(value: str) -> str:
    """Remove common campaign parameters without following redirects."""
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    if host == "tracking.tldrnewsletter.com":
        parts = parsed.path.split("/")
        if len(parts) >= 3 and parts[1] in {"CL0", "CL1", "CL2"}:
            embedded = unquote(parts[2])
            if embedded.startswith(("http://", "https://")):
                parsed = urlparse(embedded)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    ignored = {"ref", "source", "mc_cid", "mc_eid"}
    query = [
        (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in ignored
    ]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", urlencode(query), ""))


def classify_entry(url: str, title: str, excerpt: str = "") -> tuple[str, float]:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    text = f"{title} {excerpt}".lower()
    if "arxiv.org/" in url or "semanticscholar.org/paper/" in url or "/papers/" in url and host == "huggingface.co":
        return "paper", 0.99
    if any(token in host for token in ("jobs.", "greenhouse.io", "lever.co", "ashbyhq.com")):
        return "job", 0.98
    if any(token in text for token in ("sponsor", "advertise", "sign up", "unsubscribe", "refer a friend")):
        return "advertisement", 0.9
    if host == "github.com" or any(token in text for token in ("tutorial", "benchmark", "repository", "open source")):
        return "engineering", 0.78
    if any(token in text for token in ("launch", "release", "announc", "now available", "beta")):
        return "product_release", 0.72
    if any(token in host for token in ("techcrunch.com", "cnbc.com", "reuters.com", "bloomberg.com")):
        return "business_news", 0.82
    if any(token in text for token in ("why ", "opinion", "essay", "lessons", "trap")):
        return "opinion", 0.65
    return "research_news", 0.55


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href", "")
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href:
            self.anchors.append((self._href, re.sub(r"\s+", " ", " ".join(self._text)).strip()))
            self._href = ""
            self._text = []


@dataclass(frozen=True)
class _HtmlBlock:
    text: str
    anchors: tuple[tuple[str, str], ...]


class _TextBlockParser(HTMLParser):
    """Capture TLDR's semantic text-block containers without a DOM dependency."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[_HtmlBlock] = []
        self._depth = 0
        self._text: list[str] = []
        self._anchors: list[tuple[str, str]] = []
        self._href = ""
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag.lower() == "div" and "text-block" in str(attributes.get("class", "")).split() and self._depth == 0:
            self._depth = 1
            self._text = []
            self._anchors = []
            return
        if self._depth:
            self._depth += 1
            if tag.lower() == "a":
                self._href = str(attributes.get("href", ""))
                self._anchor_text = []

    def handle_data(self, data):
        if self._depth:
            self._text.append(data)
            if self._href:
                self._anchor_text.append(data)

    def handle_endtag(self, tag):
        if not self._depth:
            return
        if tag.lower() == "a" and self._href:
            label = re.sub(r"\s+", " ", " ".join(self._anchor_text)).strip()
            self._anchors.append((self._href, label))
            self._href = ""
            self._anchor_text = []
        self._depth -= 1
        if self._depth == 0:
            text = re.sub(r"\s+", " ", " ".join(self._text)).strip()
            self.blocks.append(_HtmlBlock(text, tuple(self._anchors)))

def _payload(message: Message) -> tuple[str, str]:
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk() if message.is_multipart() else (message,):
        if "attachment" in (part.get("Content-Disposition") or "").lower():
            continue
        raw = part.get_payload(decode=True)
        if not isinstance(raw, bytes):
            continue
        try:
            text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        if part.get_content_type() == "text/plain":
            plain.append(text)
        elif part.get_content_type() == "text/html":
            html.append(text)
    return "\n".join(plain), "\n".join(html)


def _subject(message: Message) -> str:
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(message.get("Subject", "")))).strip()
    except (LookupError, UnicodeDecodeError):
        return str(message.get("Subject", "")).strip()


def _direct_links(text: str) -> list[str]:
    return re.findall(r'https?://[^\s<>"\']+', text)


def _entry(title: str, url: str, excerpt: str = "") -> NewsletterEntry | None:
    canonical = canonicalize_url(url.rstrip(".,;)]}"))
    if not canonical:
        return None
    kind, confidence = classify_entry(canonical, title, excerpt)
    reason = kind if kind in {"advertisement", "job"} else ""
    return NewsletterEntry(title=title.strip() or canonical, url=canonical, excerpt=excerpt.strip(), content_type=kind, confidence=confidence, filter_reason=reason, tracked_url=url)


class NewsletterParser:
    parser_id = "generic-v1"

    def matches(self, message: Message) -> bool:
        return True

    def parse(self, message: Message) -> ParsedNewsletter:
        plain, html = _payload(message)
        subject = _subject(message) or "Newsletter item"
        links = _direct_links(plain)
        if html:
            parser = _AnchorParser(); parser.feed(html)
            links.extend(href for href, _ in parser.anchors)
        entries: list[NewsletterEntry] = []
        seen: set[str] = set()
        for link in links:
            value = _entry(subject, link, re.sub(r"\s+", " ", plain)[:500])
            if value and value.url not in seen and value.content_type == "paper":
                seen.add(value.url); entries.append(value)
        digest = hashlib.sha256((plain + "\n" + html).encode("utf-8", errors="replace")).hexdigest()
        return ParsedNewsletter(self.parser_id, subject, tuple(entries), digest)


class HuggingFaceDailyParser(NewsletterParser):
    parser_id = "huggingface-daily-v1"

    def matches(self, message: Message) -> bool:
        sender = str(message.get("From", "")).lower()
        return "huggingface.co" in sender or _subject(message).lower().startswith("daily papers of")

    def parse(self, message: Message) -> ParsedNewsletter:
        plain, html = _payload(message); subject = _subject(message)
        parser = _AnchorParser(); parser.feed(html)
        candidates = parser.anchors + [(url, "") for url in _direct_links(plain)]
        entries: list[NewsletterEntry] = []; seen: set[str] = set()
        for url, label in candidates:
            match = re.search(r"huggingface\.co/papers/(\d{4}\.\d{4,5})", url, re.I)
            if not match:
                continue
            canonical = f"https://arxiv.org/abs/{match.group(1)}"
            if canonical in seen:
                continue
            seen.add(canonical)
            entries.append(NewsletterEntry(label or f"arXiv {match.group(1)}", canonical, section="Daily Papers", content_type="paper", confidence=0.99, tracked_url=url))
        digest = hashlib.sha256((plain + "\n" + html).encode()).hexdigest()
        return ParsedNewsletter(self.parser_id, subject, tuple(entries), digest)


class TldrNewsletterParser(NewsletterParser):
    parser_id = "tldr-v1"
    _noise = re.compile(r"^(sign up|advertise|view online|unsubscribe|manage preferences|share|refer)", re.I)
    _reading_time = re.compile(r"\s*\(\s*\d+\s+minutes?\s+read\s*\)\s*$", re.I)

    @staticmethod
    def _looks_like_section(text: str) -> bool:
        value = text.strip().rstrip(":")
        lowered = value.lower()
        if not 3 <= len(value) <= 60 or len(value.split()) > 7:
            return False
        if any(token in lowered for token in ("referral", "subscribe", "advertise", "tldr swag")):
            return False
        return " & " in value or lowered in {"quick links", "miscellaneous"}

    @staticmethod
    def _section_classification(section: str, kind: str, confidence: float) -> tuple[str, float]:
        lowered = section.lower()
        if kind in {"paper", "business_news"}:
            return kind, confidence
        if "opinion" in lowered or "analysis" in lowered:
            return "opinion", max(confidence, .75)
        if "engineering" in lowered or "tutorial" in lowered or "programming" in lowered:
            return "engineering", max(confidence, .72)
        if "launch" in lowered or "tools" in lowered:
            return "product_release", max(confidence, .72)
        return kind, confidence

    def matches(self, message: Message) -> bool:
        return "tldrnewsletter.com" in str(message.get("From", "")).lower()

    def parse(self, message: Message) -> ParsedNewsletter:
        plain, html = _payload(message); subject = _subject(message)
        parser = _TextBlockParser(); parser.feed(html)
        entries: list[NewsletterEntry] = []; seen: set[str] = set()
        section = ""
        for block in parser.blocks:
            if not block.anchors and self._looks_like_section(block.text):
                section = block.text.strip().rstrip(":")
                continue
            article = next((pair for pair in block.anchors if self._reading_time.search(pair[1])), None)
            if article is None:
                continue
            url, label = article
            if "(sponsor)" in block.text.lower() or "(sponsor)" in label.lower():
                continue
            title = self._reading_time.sub("", label).strip()
            excerpt = block.text.replace(label, "", 1).strip()
            value = _entry(title, url, excerpt)
            if not value or value.url in seen or value.filter_reason:
                continue
            host = (urlparse(value.url).hostname or "").lower().removeprefix("www.")
            if host in {"linkedin.com", "twitter.com", "x.com"} or host.endswith("tldr.tech") or host.endswith("tldrnewsletter.com"):
                continue
            seen.add(value.url)
            kind, confidence = self._section_classification(
                section, value.content_type, value.confidence,
            )
            entries.append(NewsletterEntry(
                title=value.title, url=value.url, excerpt=value.excerpt,
                section=section, content_type=kind,
                confidence=confidence, filter_reason=value.filter_reason,
                tracked_url=value.tracked_url,
            ))
        digest = hashlib.sha256((plain + "\n" + html).encode()).hexdigest()
        return ParsedNewsletter(self.parser_id, subject, tuple(entries), digest)


class NewsletterParserRegistry:
    def __init__(self) -> None:
        self.parsers = (HuggingFaceDailyParser(), TldrNewsletterParser())
        self.fallback = NewsletterParser()

    def parse(self, message: Message) -> ParsedNewsletter:
        return next((parser for parser in self.parsers if parser.matches(message)), self.fallback).parse(message)
