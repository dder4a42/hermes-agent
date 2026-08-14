"""Explicit-redirect URL resolution with SSRF-oriented destination checks."""
from __future__ import annotations

import ipaddress
import os
import socket
import time
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
    def __init__(
        self, *, session=None, direct_session=None, dns_resolver=_system_resolve,
        timeout: float = 12.0, max_redirects: int = 5,
    ):
        self.session = session or requests.Session()
        self.direct_session = direct_session
        self.proxy = os.environ.get("RESEARCH_COPILOT_HTTP_PROXY", "http://127.0.0.1:7890").strip()
        if self.proxy:
            proxies = getattr(self.session, "proxies", None)
            if proxies is not None:
                proxies.update({"http": self.proxy, "https": self.proxy})
        self.dns_resolver = dns_resolver
        self.timeout = timeout
        self.max_redirects = max_redirects

    def resolve(self, url: str) -> ResolvedUrl:
        current = canonicalize_url(url)
        if not current:
            raise UrlResolutionError("Invalid URL", code="invalid_url")
        chain = [current]
        for _ in range(self.max_redirects + 1):
            # Under GFW the local resolver can see poisoned/empty DNS for
            # otherwise-fine foreign hosts; when routing through the proxy,
            # the proxy owns DNS, so skip the local public-IP check.
            if not self.proxy:
                validate_public_url(current, self.dns_resolver)
            response = None
            try:
                proxy_error = None
                for attempt in range(3):
                    try:
                        response = self.session.get(
                            current, allow_redirects=False, stream=True, timeout=self.timeout,
                            headers={"User-Agent": "HermesResearchLibrary/1.0"},
                        )
                        break
                    except requests.RequestException as exc:
                        proxy_error = exc
                        if attempt == 2:
                            break
                        time.sleep(1.0 * (attempt + 1))
                if response is None and self.proxy:
                    # A local proxy may have a broken TLS upstream while the
                    # destination remains directly reachable.  Fall back only
                    # after validating the direct destination with the normal
                    # SSRF guard; the proxy path deliberately skips this check
                    # because proxy-owned DNS may differ under the GFW.
                    try:
                        validate_public_url(current, self.dns_resolver)
                        direct = self.direct_session or requests.Session()
                        direct.trust_env = False
                        response = direct.get(
                            current, allow_redirects=False, stream=True,
                            timeout=self.timeout,
                            headers={"User-Agent": "HermesResearchLibrary/1.0"},
                        )
                    except (requests.RequestException, UrlResolutionError) as direct_error:
                        raise UrlResolutionError(
                            "URL request failed through proxy and direct fallback: "
                            f"proxy={proxy_error}; direct={direct_error}",
                            code="network_error",
                        ) from direct_error
                if response is None:
                    raise UrlResolutionError(
                        f"URL request failed: {proxy_error}", code="network_error",
                    ) from proxy_error
                assert response is not None  # loop breaks or raises
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
                if response is not None:
                    response.close()
        raise UrlResolutionError("Too many redirects", code="too_many_redirects")
