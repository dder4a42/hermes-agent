"""Gmail IMAP newsletter provider with an isolated, injectable client."""

from __future__ import annotations

import email
import imaplib
import re
import ssl
from dataclasses import dataclass
from datetime import timedelta
from email.message import Message
from html import unescape
from typing import Callable, Protocol
from urllib.parse import urlparse

from research_copilot.library import ResearchItemDraft
from research_copilot.sources.models import SourceDefinition
from research_copilot.sources.newsletters import NewsletterParserRegistry, ParsedNewsletter

from .base import FetchContext, ProviderError, ProviderItem, ProviderResult


class ImapClient(Protocol):
    def select(self, mailbox: str, readonly: bool = False): ...
    def search(self, charset, *criteria): ...
    def fetch(self, message_set, message_parts): ...
    def logout(self): ...


ImapFactory = Callable[[str, str, str, int, str], ImapClient]


class _SocksIMAP4SSL(imaplib.IMAP4_SSL):
    def __init__(self, host: str, port: int, *, proxy_url: str, timeout: int):
        self._proxy_url = proxy_url
        super().__init__(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)

    def _create_socket(self, timeout):
        try:
            import socks
        except ImportError as exc:
            raise OSError("PySocks is required for Gmail proxy_url") from exc
        proxy = urlparse(self._proxy_url)
        if proxy.scheme not in {"socks5", "socks5h"}:
            raise OSError("Gmail proxy_url must use socks5:// or socks5h://")
        raw = socks.create_connection(
            (self.host, self.port),
            timeout=timeout,
            proxy_type=socks.SOCKS5,
            proxy_addr=proxy.hostname,
            proxy_port=proxy.port or 1080,
            proxy_rdns=proxy.scheme == "socks5h",
            proxy_username=proxy.username,
            proxy_password=proxy.password,
        )
        return self.ssl_context.wrap_socket(raw, server_hostname=self.host)


def _default_factory(address: str, password: str, host: str, port: int, proxy_url: str) -> ImapClient:
    client: ImapClient
    if proxy_url:
        client = _SocksIMAP4SSL(host, port, proxy_url=proxy_url, timeout=20)
    else:
        client = imaplib.IMAP4_SSL(host, port, timeout=20)
    client.login(address, password)  # type: ignore[attr-defined]
    return client


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return str(payload or "")
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _message_body(message: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain.append(_decode_part(part))
        elif content_type == "text/html":
            value = re.sub(r"<script.*?</script>|<style.*?</style>", " ", _decode_part(part), flags=re.I | re.S)
            hrefs = " ".join(re.findall(r"href=[\"']([^\"']+)[\"']", value, flags=re.I))
            html.append(f"{unescape(re.sub(r'<[^>]+>', ' ', value))} {hrefs}")
    return "\n".join(plain or html)


def _subject(message: Message) -> str:
    from email.header import decode_header, make_header

    try:
        return str(make_header(decode_header(message.get("Subject", "")))).strip()
    except (LookupError, UnicodeDecodeError):
        return str(message.get("Subject", "")).strip()


@dataclass(frozen=True)
class _Link:
    url: str
    arxiv_id: str = ""
    semantic_scholar_id: str = ""


def _extract_links(body: str) -> list[_Link]:
    links: list[_Link] = []
    seen: set[str] = set()
    for match in re.finditer(r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?", body, re.I):
        arxiv_id = match.group(1)
        url = f"https://arxiv.org/abs/{arxiv_id}"
        if url not in seen:
            seen.add(url)
            links.append(_Link(url=url, arxiv_id=arxiv_id))
    for match in re.finditer(r"https?://(?:www\.)?semanticscholar\.org/paper/(?:[^\s<>'\")]+/)?([0-9a-f]{40})(?:[^\s<>'\"]*)?", body, re.I):
        paper_id = match.group(1)
        url = f"https://www.semanticscholar.org/paper/{paper_id}"
        if url not in seen:
            seen.add(url)
            links.append(_Link(url=url, semantic_scholar_id=paper_id))
    return links


class GmailNewsletterProvider:
    def __init__(
        self,
        *,
        address: str,
        app_password: str,
        client_factory: ImapFactory = _default_factory,
        parser_registry: NewsletterParserRegistry | None = None,
    ) -> None:
        self.address = address
        self.app_password = app_password
        self.client_factory = client_factory
        self.parser_registry = parser_registry or NewsletterParserRegistry()

    def fetch(self, source: SourceDefinition, context: FetchContext) -> ProviderResult:
        if not self.address or not self.app_password:
            raise ProviderError("Gmail credentials are not configured", code="missing_credential")
        if context.remaining_requests < 3:
            raise ProviderError(
                "Gmail requires at least 3 requests for login, select and search",
                code="budget_exhausted",
            )
        host = str(source.options.get("host", "imap.gmail.com"))
        port = int(source.options.get("port", 993))
        label = str(source.options.get("label", "ResearchFeeds"))
        proxy_url = str(source.options.get("proxy_url", ""))
        lookback_days = int(source.options.get("lookback_days", 1))
        max_messages = min(
            int(source.options.get("max_messages", 20)),
            context.remaining_requests - 3,
        )
        requests = 0
        try:
            client = self.client_factory(self.address, self.app_password, host, port, proxy_url)
            requests += 1
        except Exception as exc:
            raise ProviderError(
                f"Gmail login failed: {exc}", code="authentication_or_network_error",
                requests=1, retryable=True,
            ) from exc
        items: list[ProviderItem] = []
        filtered = 0
        seen: set[str] = set()
        try:
            status, _ = client.select(f'"{label}"', readonly=True)
            requests += 1
            if status != "OK":
                raise ProviderError(
                    f"Gmail label is unavailable: {label}", code="label_not_found",
                    requests=requests,
                )
            since = (context.started_at - timedelta(days=max(0, lookback_days))).strftime("%d-%b-%Y")
            status, result = client.search(None, f"(SINCE {since})")
            requests += 1
            if status != "OK":
                raise ProviderError("Gmail search failed", code="search_error", requests=requests, retryable=True)
            message_ids = result[0].split() if result and result[0] else []
            for message_id in message_ids[-max_messages:] if max_messages else ():
                if len(items) >= context.remaining_items:
                    break
                try:
                    status, payload = client.fetch(message_id, "(RFC822)")
                    requests += 1
                    if status != "OK" or not payload or not isinstance(payload[0], tuple):
                        filtered += 1
                        continue
                    message = email.message_from_bytes(payload[0][1])
                    parsed = self.parser_registry.parse(message)
                    if not parsed.entries:
                        filtered += 1
                        continue
                    for entry in parsed.entries:
                        if entry.filter_reason:
                            filtered += 1
                            continue
                        arxiv_match = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", entry.url, re.I)
                        semantic_match = re.search(r"semanticscholar\.org/paper/(?:[^/]+/)?([0-9a-f]{40})", entry.url, re.I)
                        arxiv_id = arxiv_match.group(1) if arxiv_match else ""
                        semantic_id = semantic_match.group(1) if semantic_match else ""
                        identity = arxiv_id or semantic_id or entry.url
                        if identity in seen:
                            continue
                        seen.add(identity)
                        items.append(ProviderItem(item=ResearchItemDraft(
                            title=entry.title,
                            item_type=entry.content_type,
                            summary=entry.excerpt[:2000],
                            url=entry.url,
                            arxiv_id=arxiv_id,
                            semantic_scholar_id=semantic_id,
                            metadata={
                                "newsletter_label": label,
                                "newsletter_subject": parsed.subject,
                                "newsletter_parser": parsed.parser_id,
                                "newsletter_body_hash": parsed.body_hash,
                                "newsletter_section": entry.section,
                                "classification_confidence": entry.confidence,
                                "discovered_via": source.id,
                                "newsletter_uid": message_id.decode("ascii", errors="replace") if isinstance(message_id, bytes) else str(message_id),
                                "newsletter_message_id": str(message.get("Message-ID", "")),
                                "newsletter_sender": str(message.get("From", "")),
                                "newsletter_received_at": str(message.get("Date", "")),
                                "newsletter_tracked_url": entry.tracked_url,
                            },
                        )))
                        if len(items) >= context.remaining_items:
                            break
                except (imaplib.IMAP4.error, OSError, ValueError, TypeError):
                    filtered += 1
                    continue
        except ProviderError:
            raise
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ProviderError(
                f"Gmail IMAP failed: {exc}", code="imap_error",
                requests=requests, retryable=True,
            ) from exc
        finally:
            try:
                client.logout()
            except Exception:
                pass
        return ProviderResult(items=tuple(items), requests=requests, filtered=filtered)
