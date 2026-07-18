from datetime import datetime, timezone

from research_copilot.library import LibraryRepository, connect_library, initialize_library


def test_newsletter_issue_staging_is_uid_idempotent_and_preserves_entry_provenance(tmp_path):
    connection = connect_library(tmp_path / "library.db")
    initialize_library(connection, migrated_at="2026-07-17T00:00:00+00:00")
    repository = LibraryRepository(connection)
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    repository.upsert_source(
        source_id="gmail", provider="gmail_newsletter", display_name="Gmail",
        source_type="newsletter", tier=.7, now=now,
    )
    entries = ({
        "title": "Agent launch", "canonical_url": "https://example.com/agent",
        "tracked_url": "https://tracking.example/x", "content_type": "product_release",
        "classification_confidence": .8,
    },)
    first = repository.record_newsletter_issue(
        source_id="gmail", mailbox="ResearchFeeds", uid_validity="1", uid="42",
        body_hash="abc", parser_id="tldr-v1", subject="Issue", created_at=now,
        entries=entries,
    )
    second = repository.record_newsletter_issue(
        source_id="gmail", mailbox="ResearchFeeds", uid_validity="1", uid="42",
        body_hash="changed", parser_id="tldr-v2", subject="Duplicate", created_at=now,
        entries=(),
    )
    assert second == first
    assert connection.execute("SELECT count(*) FROM newsletter_issues").fetchone()[0] == 1
    entry = connection.execute("SELECT * FROM newsletter_entries").fetchone()
    assert entry["publisher_domain"] == "example.com"
    assert entry["tracked_url"] == "https://tracking.example/x"
    connection.close()
