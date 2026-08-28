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
    def uid(self, command: str, *args): ...
    def response(self, code: str): ...
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


def _selected_number(client: ImapClient, code: str) -> str:
    """Return one numeric SELECT response such as UIDVALIDITY or UIDNEXT."""
    try:
        _name, values = client.response(code)
    except (AttributeError, imaplib.IMAP4.error, OSError):
        return ""
    if not values:
        return ""
    value = values[-1]
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    match = re.search(r"\d+", str(value))
    return match.group(0) if match else ""


def _uid_text(value: bytes | str) -> str:
    return value.decode("ascii", errors="replace") if isinstance(value, bytes) else str(value)


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
        from_domains = [str(d).strip() for d in (source.options.get("from_domains") or ()) if str(d).strip()]
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
        partial_error: tuple[str, str] | None = None
        try:
            mailbox = "INBOX" if label.upper() == "INBOX" else f'"{label}"'
            status, _ = client.select(mailbox, readonly=True)
            requests += 1
            if status != "OK":
                raise ProviderError(
                    f"Gmail label is unavailable: {label}", code="label_not_found",
                    requests=requests,
                )
            uid_validity = _selected_number(client, "UIDVALIDITY")
            uid_next = _selected_number(client, "UIDNEXT")
            stored_uid_validity = str(context.source_state.get("uid_validity") or "")
            stored_last_uid = str(context.source_state.get("last_uid") or "")
            incremental = bool(
                uid_validity and stored_uid_validity == uid_validity
                and stored_last_uid.isdigit()
            )
            since = (context.started_at - timedelta(days=max(0, lookback_days))).strftime("%d-%b-%Y")
            # 可选发件人域过滤：每个域单独 SEARCH（服务器端筛选），再合并去重。
            # 空列表 = 拉取 mailbox 内全部邮件（ResearchFeeds label 模式）。
            message_ids: list[bytes] = []
            message_id_seen: set[bytes] = set()
            uid_floor = int(stored_last_uid) + 1 if incremental else None

            def search_criteria(domain: str | None = None) -> str:
                pieces = [f"UID {uid_floor}:*"] if uid_floor is not None else [f"SINCE {since}"]
                if domain:
                    pieces.append(f'FROM "{domain}"')
                return f"({' '.join(pieces)})"

            def search_messages(domain: str | None = None):
                criteria = search_criteria(domain)
                if uid_validity:
                    return client.uid("SEARCH", None, criteria)
                return client.search(None, criteria)

            if from_domains:
                for domain in from_domains:
                    status, result = search_messages(domain)
                    requests += 1
                    if status != "OK":
                        raise ProviderError("Gmail search failed", code="search_error", requests=requests, retryable=True)
                    for message_id in (result[0].split() if result and result[0] else []):
                        if uid_floor is not None and int(_uid_text(message_id)) < uid_floor:
                            continue
                        if message_id not in message_id_seen:
                            message_id_seen.add(message_id)
                            message_ids.append(message_id)
            else:
                status, result = search_messages()
                requests += 1
                if status != "OK":
                    raise ProviderError("Gmail search failed", code="search_error", requests=requests, retryable=True)
                message_ids = result[0].split() if result and result[0] else []
                if uid_floor is not None:
                    message_ids = [
                        value for value in message_ids
                        if int(_uid_text(value)) >= uid_floor
                    ]
            message_ids.sort(key=lambda value: int(_uid_text(value)))
            selected_ids = (
                message_ids[:max_messages] if incremental else message_ids[-max_messages:]
            ) if max_messages else ()
            last_processed_uid = int(stored_last_uid) if incremental else 0
            for message_id in selected_ids:
                if len(items) >= context.remaining_items:
                    break
                try:
                    status, payload = (
                        client.uid("FETCH", message_id, "(RFC822)")
                        if uid_validity else client.fetch(message_id, "(RFC822)")
                    )
                    requests += 1
                    if status != "OK" or not payload or not isinstance(payload[0], tuple):
                        filtered += 1
                        partial_error = (
                            "message_fetch_error",
                            f"Gmail UID {_uid_text(message_id)} could not be fetched",
                        )
                        break
                    message = email.message_from_bytes(payload[0][1])
                    parsed = self.parser_registry.parse(message)
                    if not parsed.entries:
                        filtered += 1
                        last_processed_uid = int(_uid_text(message_id))
                        continue
                    message_items: list[ProviderItem] = []
                    message_identities: set[str] = set()
                    for entry in parsed.entries:
                        if entry.filter_reason:
                            filtered += 1
                            continue
                        arxiv_match = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", entry.url, re.I)
                        semantic_match = re.search(r"semanticscholar\.org/paper/(?:[^/]+/)?([0-9a-f]{40})", entry.url, re.I)
                        arxiv_id = arxiv_match.group(1) if arxiv_match else ""
                        semantic_id = semantic_match.group(1) if semantic_match else ""
                        identity = arxiv_id or semantic_id or entry.url
                        if identity in seen or identity in message_identities:
                            continue
                        message_identities.add(identity)
                        message_items.append(ProviderItem(item=ResearchItemDraft(
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
                                "newsletter_uid": _uid_text(message_id),
                                "newsletter_uid_validity": uid_validity,
                                "newsletter_message_id": str(message.get("Message-ID", "")),
                                "newsletter_sender": str(message.get("From", "")),
                                "newsletter_received_at": str(message.get("Date", "")),
                                "newsletter_tracked_url": entry.tracked_url,
                            },
                        )))
                    if len(items) + len(message_items) > context.remaining_items:
                        # A newsletter issue is staged atomically. Do not move the
                        # UID cursor past a partially emitted issue.
                        partial_error = (
                            "item_budget",
                            f"Gmail UID {_uid_text(message_id)} exceeds the remaining item budget",
                        )
                        break
                    seen.update(message_identities)
                    items.extend(message_items)
                    last_processed_uid = int(_uid_text(message_id))
                except (imaplib.IMAP4.error, OSError, ValueError, TypeError):
                    filtered += 1
                    partial_error = (
                        "message_parse_error",
                        f"Gmail UID {_uid_text(message_id)} could not be parsed",
                    )
                    break
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
        state_updates = {}
        if uid_validity:
            state_updates["uid_validity"] = uid_validity
            if incremental:
                state_updates["last_uid"] = str(last_processed_uid)
            elif last_processed_uid:
                state_updates["last_uid"] = str(last_processed_uid)
            elif uid_next.isdigit():
                state_updates["last_uid"] = str(max(0, int(uid_next) - 1))
            else:
                state_updates["last_uid"] = "0"
        metrics = {
            "incremental": incremental,
            "uid_validity_changed": bool(
                uid_validity and stored_uid_validity
                and stored_uid_validity != uid_validity
            ),
            "searched_uid_count": len(message_ids),
            "selected_uid_count": len(selected_ids),
            "last_processed_uid": last_processed_uid,
        }
        return ProviderResult(
            items=tuple(items), requests=requests, filtered=filtered,
            state_updates=state_updates,
            error_code=partial_error[0] if partial_error else None,
            error_message=partial_error[1] if partial_error else None,
            metrics=metrics,
        )
