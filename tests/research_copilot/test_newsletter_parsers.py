from email.message import EmailMessage

from research_copilot.sources.newsletters import (
    HuggingFaceDailyParser, NewsletterParserRegistry, TldrNewsletterParser,
    canonicalize_url, classify_entry,
)


def _html_message(sender: str, subject: str, html: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    message.set_content("HTML newsletter")
    message.add_alternative(html, subtype="html")
    return message


def test_huggingface_daily_recovers_arxiv_identity_and_deduplicates_links():
    message = _html_message(
        "daily_papers_digest@notifications.huggingface.co", "Daily papers of 17 Jul 2026",
        "<a href='https://huggingface.co/papers/2607.01234?utm_source=email'>Agent Memory</a>"
        "<a href='https://huggingface.co/papers/2607.01234'>PDF</a>",
    )
    parsed = NewsletterParserRegistry().parse(message)
    assert parsed.parser_id == "huggingface-daily-v1"
    assert len(parsed.entries) == 1
    assert parsed.entries[0].title == "Agent Memory"
    assert parsed.entries[0].url == "https://arxiv.org/abs/2607.01234"
    assert parsed.entries[0].content_type == "paper"


def test_tldr_parser_splits_entries_and_filters_navigation_ads_and_social_links():
    message = _html_message(
        "dan@tldrnewsletter.com", "Agent newsletter",
        "<div class='text-block'>BIG TECH &amp; STARTUPS</div>"
        "<div class='text-block'><span><a href='https://example.com/launch?utm_source=tldr'>"
        "New agent launch (3 minute read)</a></span><p>The system adds durable agent memory.</p></div>"
        "<div class='text-block'><span><a href='https://jobs.ashbyhq.com/acme'>AI jobs</a></span></div>"
        "<div class='text-block'><a href='https://linkedin.com/share'>Share</a></div>"
        "<div class='text-block'><a href='https://advertise.tldr.tech'>Advertise</a></div>",
    )
    parsed = TldrNewsletterParser().parse(message)
    assert [(entry.title, entry.url, entry.content_type) for entry in parsed.entries] == [
        ("New agent launch", "https://example.com/launch", "product_release"),
    ]
    assert parsed.entries[0].section == "BIG TECH & STARTUPS"
    assert parsed.entries[0].excerpt == "The system adds durable agent memory."


def test_tldr_parser_discards_sponsor_and_deduplicates_article_ctas():
    message = _html_message(
        "dan@tldrnewsletter.com", "Agent newsletter",
        "<div class='text-block'><a href='https://sponsor.example'>Tool (Sponsor)</a></div>"
        "<div class='text-block'><a href='https://example.com/a?utm_source=one'>A (2 minute read)</a>Summary A</div>"
        "<div class='text-block'><a href='https://example.com/a?utm_source=two'>A again (2 minute read)</a>Read more</div>",
    )
    parsed = TldrNewsletterParser().parse(message)
    assert len(parsed.entries) == 1
    assert parsed.entries[0].url == "https://example.com/a"


def test_tldr_parser_learns_section_shape_and_uses_it_for_classification():
    message = _html_message(
        "dan@tldrnewsletter.com", "Agent newsletter",
        "<div class='text-block'>Deep Dives &amp; Analysis</div>"
        "<div class='text-block'><a href='https://example.com/essay'>"
        "Agent scaling (8 minute read)</a>A detailed argument about scaling.</div>",
    )
    entry = TldrNewsletterParser().parse(message).entries[0]
    assert entry.section == "Deep Dives & Analysis"
    assert entry.content_type == "opinion"
    assert entry.confidence == 0.75


def test_url_canonicalization_and_classification_are_storage_neutral():
    assert canonicalize_url("HTTPS://Arxiv.org/abs/2607.01234?utm_campaign=x") == "https://arxiv.org/abs/2607.01234"
    assert classify_entry("https://arxiv.org/abs/2607.01234", "Paper") == ("paper", 0.99)
    assert classify_entry("https://jobs.ashbyhq.com/acme", "Role")[0] == "job"


def test_tldr_tracking_url_is_unwrapped_without_network_access():
    tracked = (
        "https://tracking.tldrnewsletter.com/CL0/"
        "https:%2F%2Ftechcrunch.com%2Fstory%3Futm_source=tldrai/1/message/signature"
    )
    assert canonicalize_url(tracked) == "https://techcrunch.com/story"
