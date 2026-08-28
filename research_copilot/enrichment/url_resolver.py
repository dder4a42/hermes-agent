"""Explicit-redirect URL resolution with SSRF-oriented destination checks."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests

from research_copilot.sources.newsletters import canonicalize_url


class UrlResolutionError(RuntimeError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedUrl:
    input_url: str
    final_url: str
    redirect_chain: tuple[str, ...]
    status_code: int
    content_type: str = ""


def _system_resolve(host: str, port: int) -> tuple[str, ...]:
    return tuple(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))


def validate_public_url(url: str, resolver: Callable[[str, int], tuple[str, ...]] = _system_resolve) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UrlResolutionError("Only absolute HTTP(S) URLs are allowed", code="invalid_url")
    if parsed.username or parsed.password:
        raise UrlResolutionError("URL credentials are not allowed", code="unsafe_url")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = resolver(parsed.hostname, port)
    except OSError as exc:
        raise UrlResolutionError(f"DNS resolution failed: {exc}", code="dns_error") from exc
    if not addresses:
        raise UrlResolutionError("DNS returned no addresses", code="dns_error")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise UrlResolutionError(f"Non-public destination rejected: {address}", code="unsafe_destination")


class UrlResolver:
    def __init__(self, *, session=None, dns_resolver=_system_resolve, timeout: float = 5.0, max_redirects: int = 5):
        self.session = session or requests.Session()
        self.dns_resolver = dns_resolver
        self.timeout = timeout
        self.max_redirects = max_redirects

    def resolve(self, url: str) -> ResolvedUrl:
        current = canonicalize_url(url)
        if not current:
            raise UrlResolutionError("Invalid URL", code="invalid_url")
        chain = [current]
        for _ in range(self.max_redirects + 1):
            validate_public_url(current, self.dns_resolver)
            try:
                response = self.session.get(
                    current, allow_redirects=False, stream=True, timeout=self.timeout,
                    headers={"User-Agent": "HermesResearchLibrary/1.0"},
                )
            except requests.RequestException as exc:
                raise UrlResolutionError(f"URL request failed: {exc}", code="network_error") from exc
            try:
                status = int(response.status_code)
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    if not location:
                        raise UrlResolutionError("Redirect has no Location", code="invalid_redirect")
                    current = canonicalize_url(urljoin(current, location))
                    if not current or current in chain:
                        raise UrlResolutionError("Redirect loop or invalid destination", code="redirect_loop")
                    chain.append(current)
                    continue
                return ResolvedUrl(
                    input_url=url, final_url=current, redirect_chain=tuple(chain),
                    status_code=status, content_type=response.headers.get("Content-Type", "").split(";", 1)[0],
                )
            finally:
                response.close()
        raise UrlResolutionError("Too many redirects", code="too_many_redirects")
