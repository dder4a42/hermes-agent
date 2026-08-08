from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from research_copilot.health import build_doctor_report, render_doctor_report
from research_copilot.library import LibraryRepository, connect_library, initialize_library
from research_copilot.library.database import SCHEMA_VERSION


def test_doctor_reports_code_config_database_and_override_provenance(tmp_path):
    database = tmp_path / "library.db"
    catalog = tmp_path / "sources.yaml"
    topics = tmp_path / "topics.yaml"
    catalog.write_text(
        "schema_version: 1\n"
        "sources:\n"
        "  - id: rss\n"
        "    provider: rss\n"
        "    display_name: RSS\n"
        "    type: feed\n"
        "    enabled: true\n"
        "    tier: 0.8\n"
        "    topics: ['*']\n"
        "    budget: {max_requests: 1, max_items: 1}\n"
        "    options: {feed_url: 'https://example.test/feed'}\n"
    )
    topics.write_text("topics: []\n")
    override = tmp_path / "home" / "skills" / "research" / "paper" / "SKILL.md"
    override.parent.mkdir(parents=True)
    override.write_text("custom")
    connection = connect_library(database)
    initialize_library(connection, migrated_at="2026-07-17T00:00:00+00:00")
    repository = LibraryRepository(connection)
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    repository.upsert_source(
        source_id="rss", provider="rss", display_name="RSS",
        source_type="feed", tier=0.8, now=now,
    )
    run_id = repository.start_source_run(source_id="rss", started_at=now)
    repository.finish_source_run(run_id, status="success", finished_at=now)
    try:
        report = build_doctor_report(
            connection, database_path=database, catalog_path=catalog,
            topics_path=topics, project_root=tmp_path,
            hermes_home=tmp_path / "home", hermes_version="1.2.3",
            git_commit="abc123",
        )
        assert report.schema_version == SCHEMA_VERSION
        assert report.enabled_sources == ("rss",)
        assert report.catalog_hash == hashlib.sha256(catalog.read_bytes()).hexdigest()
        assert report.topics_hash == hashlib.sha256(topics.read_bytes()).hexdigest()
        assert report.last_source_run_at == now.isoformat()
        assert report.item_count == 0
        assert report.recommendation_count == 0
        assert report.evidence_review_states == ()
        assert report.merge_evidence_count == 0
        assert report.user_skill_override == str(override)
        rendered = render_doctor_report(report)
        assert "Hermes version: 1.2.3" in rendered
        assert "Git commit: abc123" in rendered
        assert "Enabled sources: rss" in rendered
        assert "Library items: 0" in rendered
        assert "Profile evidence: none" in rendered
    finally:
        connection.close()


def test_doctor_uses_catalog_state_when_database_source_row_is_stale(tmp_path):
    database = tmp_path / "library.db"
    catalog = tmp_path / "sources.yaml"
    topics = tmp_path / "topics.yaml"
    catalog.write_text(
        "schema_version: 1\n"
        "sources:\n"
        "  - id: alphaxiv\n"
        "    provider: alphaxiv\n"
        "    display_name: AlphaXiv\n"
        "    type: paper_discussion\n"
        "    enabled: false\n"
        "    tier: 0.8\n"
        "    topics: ['*']\n"
        "    budget: {max_requests: 1, max_items: 1}\n"
        "    options: {}\n"
    )
    topics.write_text("topics: []\n")
    connection = connect_library(database)
    initialize_library(connection, migrated_at="2026-07-17T00:00:00+00:00")
    repository = LibraryRepository(connection)
    repository.upsert_source(
        source_id="alphaxiv", provider="alphaxiv", display_name="AlphaXiv",
        source_type="paper_discussion", tier=0.8,
        now=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    try:
        report = build_doctor_report(
            connection, database_path=database, catalog_path=catalog,
            topics_path=topics, project_root=tmp_path,
            hermes_home=tmp_path / "home", hermes_version="1.2.3",
            git_commit="abc123",
        )
        assert report.enabled_sources == ()
    finally:
        connection.close()


def test_doctor_marks_missing_config_and_no_runtime_state(tmp_path):
    database = tmp_path / "library.db"
    connection = connect_library(database)
    initialize_library(connection, migrated_at="2026-07-17T00:00:00+00:00")
    try:
        report = build_doctor_report(
            connection, database_path=database,
            catalog_path=tmp_path / "missing-sources.yaml",
            topics_path=tmp_path / "missing-topics.yaml",
            project_root=tmp_path, hermes_home=tmp_path / "home",
            hermes_version="1.2.3", git_commit="unknown",
        )
        assert report.catalog_hash == "missing"
        assert report.topics_hash == "missing"
        assert report.last_source_run_at is None
        assert report.user_skill_override is None
        assert "Last source run: never" in render_doctor_report(report)
    finally:
        connection.close()


def test_doctor_does_not_report_profile_symlink_to_tracked_skill_as_override(tmp_path):
    project = tmp_path / "project"
    tracked = project / "skills" / "research" / "paper" / "SKILL.md"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked")
    home = tmp_path / "home"
    linked = home / "skills" / "research" / "paper"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(tracked.parent)
    database = tmp_path / "library.db"
    connection = connect_library(database)
    initialize_library(connection, migrated_at="2026-07-17T00:00:00+00:00")
    try:
        report = build_doctor_report(
            connection, database_path=database,
            catalog_path=tmp_path / "sources.yaml", topics_path=tmp_path / "topics.yaml",
            project_root=project, hermes_home=home,
            hermes_version="1.2.3", git_commit="abc123",
        )
        assert report.user_skill_override is None
    finally:
        connection.close()
