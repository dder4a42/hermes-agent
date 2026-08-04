"""Shared HTTP fetching with optional Mihomo proxy routing.

The legacy ``paper_fetch.py`` pipeline routed GFW-blocked sources through a
Mihomo HTTP proxy configured via ``RESEARCH_COPILOT_HTTP_PROXY``; the v2
library pipeline lost that during the rewrite. All urllib-based providers
and the enrichment ``UrlResolver`` share this helper so a single env var
restores proxy routing everywhere.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from typing import Mapping


def proxy_url() -> str:
    """Mihomo/HTTP proxy URL, or an empty string for direct connections.

    Defaults to the legacy paper_fetch.py value (``http://127.0.0.1:7890``,
    the Mihomo mixed port on this host); override or disable via the
    ``RESEARCH_COPILOT_HTTP_PROXY`` env var.
    """
    return os.environ.get("RESEARCH_COPILOT_HTTP_PROXY", "http://127.0.0.1:7890").strip()


def fetch(
    url: str,
    *,
    timeout: int = 20,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    method: str | None = None,
    retries: int = 3,
) -> bytes:
    """Fetch ``url`` through the configured proxy with transient retries.

    HTTP status errors (4xx/5xx) re-raise immediately; connection/SSL
    failures retry up to ``retries`` times because Mihomo's AUTO proxy
    group rotates upstream nodes of varying reliability.
    """
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    proxy = proxy_url()
    opener = (
        urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
        )
        if proxy
        else urllib.request.build_opener()
    )
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise urllib.error.URLError("fetch failed")  # pragma: no cover
