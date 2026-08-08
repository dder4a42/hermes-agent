from __future__ import annotations

import http.client
import urllib.error

import pytest


class _Response:
    def __init__(self, outcome, *, status=200, headers=None):
        self.outcome = outcome
        self.status = status
        self.headers = headers or {}

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Opener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        return _Response(self.outcomes.pop(0))


def test_shared_fetch_retries_truncated_responses(monkeypatch):
    from research_copilot import net

    opener = _Opener([http.client.IncompleteRead(b"partial", 100), b"complete"])
    monkeypatch.setattr(net, "proxy_url", lambda: "")
    monkeypatch.setattr(net.urllib.request, "build_opener", lambda *args: opener)
    monkeypatch.setattr(net.time, "sleep", lambda seconds: None)

    assert net.fetch("https://example.com", retries=2) == b"complete"
    assert opener.calls == 2


def test_shared_fetch_reports_persistent_truncation_as_url_error(monkeypatch):
    from research_copilot import net

    opener = _Opener([http.client.IncompleteRead(b"partial", 100)])
    monkeypatch.setattr(net, "proxy_url", lambda: "")
    monkeypatch.setattr(net.urllib.request, "build_opener", lambda *args: opener)

    with pytest.raises(urllib.error.URLError, match="truncated HTTP response"):
        net.fetch("https://example.com", retries=1)


def test_shared_fetch_returns_304_metadata_for_conditional_requests(monkeypatch):
    from research_copilot import net

    class NotModifiedOpener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 304, "not modified", {"ETag": '"v1"'}, None,
            )

    monkeypatch.setattr(net, "proxy_url", lambda: "")
    monkeypatch.setattr(net.urllib.request, "build_opener", lambda *args: NotModifiedOpener())
    response = net.fetch("https://example.com/feed", return_response=True)
    assert response.status == 304
    assert response.body == b""
    assert response.headers["ETag"] == '"v1"'
