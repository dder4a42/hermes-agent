from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from research_copilot.sources import ProviderError, SourceDefinition
from research_copilot.sources.providers.base import FetchContext
from research_copilot.sources.providers.gmail_newsletter import GmailNewsletterProvider


S2_ID = "a" * 40


class FakeImap:
    def __init__(self, messages, *, select_status="OK", search_status="OK"):
        self.messages = messages
        self.select_status = select_status
        self.search_status = search_status
        self.selected = None
        self.search_criteria = None
        self.logged_out = False

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return self.select_status, [b""]

    def search(self, charset, *criteria):
        self.search_criteria = criteria
        ids = b" ".join(str(i).encode() for i in range(1, len(self.messages) + 1))
        return self.search_status, [ids]

    def fetch(self, message_id, parts):
        index = int(message_id) - 1
        return "OK", [(b"RFC822", self.messages[index])]

    def logout(self):
        self.logged_out = True


def _message(subject, body, *, html=False):
    message = EmailMessage()
    message["Subject"] = subject
    if html:
        message.set_content("fallback")
        message.add_alternative(body, subtype="html")
    else:
        message.set_content(body)
    return message.as_bytes()


def _source(**options):
    return SourceDefinition(
        id="gmail", provider="gmail_newsletter", display_name="ResearchFeeds",
        source_type="newsletter", enabled=True, tier=0.9,
        topics=("*",), options=options,
    )


def _context(requests=10, items=10):
    return FetchContext(
        datetime(2026, 7, 17, tzinfo=timezone.utc), (), requests, items,
    )


def test_gmail_contract_extracts_arxiv_and_semantic_scholar_links():
    body = (
        "Agent research https://arxiv.org/abs/2607.01234v2 and "
        f"https://www.semanticscholar.org/paper/title/{S2_ID}?x=1"
    )
    client = FakeImap([_message("Weekly Agent Papers", body)])
    factory_calls = []
    provider = GmailNewsletterProvider(
        address="alice@example.com", app_password="secret",
        client_factory=lambda *args: factory_calls.append(args) or client,
    )
    result = provider.fetch(
        _source(label="ResearchFeeds", lookback_days=2, max_messages=5), _context(),
    )
    assert result.requests == 4  # login, select, search, one fetch
    assert len(result.items) == 2
    assert result.items[0].item.arxiv_id == "2607.01234"
    assert result.items[1].item.semantic_scholar_id == S2_ID
    assert {item.item.title for item in result.items} == {"Weekly Agent Papers"}
    assert client.selected == ('"ResearchFeeds"', True)
    assert client.search_criteria == ("(SINCE 15-Jul-2026)",)
    assert client.logged_out is True
    assert factory_calls[0][:4] == ("alice@example.com", "secret", "imap.gmail.com", 993)


def test_gmail_uses_html_when_plain_body_is_absent():
    raw = EmailMessage()
    raw["Subject"] = "HTML Newsletter"
    raw.set_content("<p>Paper <a href='https://arxiv.org/abs/2607.09999'>link</a></p>", subtype="html")
    client = FakeImap([raw.as_bytes()])
    result = GmailNewsletterProvider(
        address="a", app_password="p", client_factory=lambda *_: client,
    ).fetch(_source(), _context())
    assert result.items[0].item.arxiv_id == "2607.09999"


def test_gmail_missing_credentials_never_connects():
    with pytest.raises(ProviderError) as captured:
        GmailNewsletterProvider(
            address="", app_password="", client_factory=lambda *_: pytest.fail("connected"),
        ).fetch(_source(), _context())
    assert captured.value.code == "missing_credential"
    assert captured.value.requests == 0


def test_gmail_budget_accounts_for_login_select_search_and_fetches():
    messages = [
        _message("One", "https://arxiv.org/abs/2607.00001"),
        _message("Two", "https://arxiv.org/abs/2607.00002"),
    ]
    client = FakeImap(messages)
    result = GmailNewsletterProvider(
        address="a", app_password="p", client_factory=lambda *_: client,
    ).fetch(_source(max_messages=20), _context(requests=4))
    assert result.requests == 4
    assert len(result.items) == 1


def test_gmail_requires_minimum_connection_budget():
    with pytest.raises(ProviderError) as captured:
        GmailNewsletterProvider(
            address="a", app_password="p", client_factory=lambda *_: pytest.fail("connected"),
        ).fetch(_source(), _context(requests=2))
    assert captured.value.code == "budget_exhausted"


def test_gmail_label_failure_logs_out_and_preserves_request_count():
    client = FakeImap([], select_status="NO")
    with pytest.raises(ProviderError) as captured:
        GmailNewsletterProvider(
            address="a", app_password="p", client_factory=lambda *_: client,
        ).fetch(_source(label="Missing"), _context())
    assert captured.value.code == "label_not_found"
    assert captured.value.requests == 2
    assert client.logged_out is True
