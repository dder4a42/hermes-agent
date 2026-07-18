from datetime import datetime, timezone

import pytest

from research_copilot.enrichment import PageMetadataExtractor, UrlResolutionError, UrlResolver
from research_copilot.enrichment.url_resolver import validate_public_url


class Response:
    def __init__(self, status=200, *, headers=None, body=b""):
        self.status_code=status; self.headers=headers or {}; self.body=body; self.encoding="utf-8"; self.closed=False
    def close(self): self.closed=True
    def raise_for_status(self):
        if self.status_code >= 400: raise ValueError("http error")
    def iter_content(self, _size): return iter((self.body,))


class Session:
    def __init__(self, responses): self.responses=list(responses); self.calls=[]
    def get(self, url, **kwargs): self.calls.append((url,kwargs)); return self.responses.pop(0)


PUBLIC = lambda host, port: ("93.184.216.34",)


def test_resolver_validates_every_redirect_and_canonicalizes_destination():
    session=Session([Response(302,headers={"Location":"https://example.org/a?utm_source=x"}),Response(200,headers={"Content-Type":"text/html; charset=utf-8"})])
    result=UrlResolver(session=session,dns_resolver=PUBLIC).resolve("https://short.example/x")
    assert result.final_url == "https://example.org/a"
    assert result.redirect_chain == ("https://short.example/x", "https://example.org/a")
    assert all(call[1]["allow_redirects"] is False for call in session.calls)


def test_ssrf_guard_rejects_private_and_credentialed_destinations():
    with pytest.raises(UrlResolutionError, match="Non-public"):
        validate_public_url("http://example.test/x", lambda *_: ("127.0.0.1",))
    with pytest.raises(UrlResolutionError, match="credentials"):
        validate_public_url("https://user:pass@example.com", PUBLIC)


def test_page_metadata_prefers_json_ld_and_canonical_link():
    body=b'''<html><head><title>Fallback</title><link rel="canonical" href="/paper" />
    <script type="application/ld+json">{"@type":"ScholarlyArticle","headline":"Agent Paper","description":"Abstract","author":{"name":"Ada"},"datePublished":"2026-07-17"}</script></head></html>'''
    metadata=PageMetadataExtractor(session=Session([Response(headers={"Content-Type":"text/html"},body=body)]),dns_resolver=PUBLIC).fetch("https://example.org/x")
    assert metadata.canonical_url == "https://example.org/paper"
    assert (metadata.title,metadata.author,metadata.page_type) == ("Agent Paper","Ada","ScholarlyArticle")
