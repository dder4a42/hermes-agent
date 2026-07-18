"""Connection and schema management for the profile-scoped Research Library."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    migrated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    tier REAL NOT NULL CHECK (tier >= 0 AND tier <= 1),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_runs (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    requests INTEGER NOT NULL DEFAULT 0 CHECK (requests >= 0),
    fetched_count INTEGER NOT NULL DEFAULT 0 CHECK (fetched_count >= 0),
    new_count INTEGER NOT NULL DEFAULT 0 CHECK (new_count >= 0),
    merged_count INTEGER NOT NULL DEFAULT 0 CHECK (merged_count >= 0),
    unchanged_count INTEGER NOT NULL DEFAULT 0 CHECK (unchanged_count >= 0),
    filtered_count INTEGER NOT NULL DEFAULT 0 CHECK (filtered_count >= 0),
    error_code TEXT,
    error_message TEXT,
    metrics_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS research_items (
    id TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    authors_json TEXT NOT NULL DEFAULT '[]',
    published_at TEXT,
    first_discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'discovered',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS item_identifiers (
    item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
    scheme TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (scheme, value)
);
CREATE TABLE IF NOT EXISTS item_sources (
    item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(id),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    discovery_count INTEGER NOT NULL DEFAULT 1,
    last_source_run_id TEXT REFERENCES source_runs(id),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (item_id, source_id)
);
CREATE TABLE IF NOT EXISTS item_topics (
    item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
    topic_id TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    matched_terms_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (item_id, topic_id)
);
CREATE TABLE IF NOT EXISTS recommendations (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL UNIQUE REFERENCES research_items(id),
    recommended_at TEXT NOT NULL,
    score REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    score_breakdown_json TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS feedback_events (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES research_items(id),
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS newsletter_issues (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    mailbox TEXT NOT NULL,
    uid_validity TEXT NOT NULL DEFAULT '',
    uid TEXT NOT NULL,
    message_id TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    received_at TEXT,
    body_hash TEXT NOT NULL,
    parser_id TEXT NOT NULL,
    parse_status TEXT NOT NULL CHECK (parse_status IN ('parsed', 'partial', 'failed')),
    created_at TEXT NOT NULL,
    UNIQUE(source_id, mailbox, uid_validity, uid)
);
CREATE TABLE IF NOT EXISTS newsletter_entries (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES newsletter_issues(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    section TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    excerpt TEXT NOT NULL DEFAULT '',
    tracked_url TEXT NOT NULL DEFAULT '',
    canonical_url TEXT NOT NULL DEFAULT '',
    publisher_domain TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL,
    classification_confidence REAL NOT NULL CHECK (classification_confidence >= 0 AND classification_confidence <= 1),
    filter_reason TEXT NOT NULL DEFAULT '',
    research_item_id TEXT REFERENCES research_items(id),
    UNIQUE(issue_id, position)
);
CREATE TABLE IF NOT EXISTS url_resolutions (
    input_url TEXT PRIMARY KEY,
    final_url TEXT NOT NULL DEFAULT '',
    redirect_chain_json TEXT NOT NULL DEFAULT '[]',
    status_code INTEGER,
    content_type TEXT NOT NULL DEFAULT '',
    resolved_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    error_code TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1)
);
CREATE TABLE IF NOT EXISTS page_metadata (
    canonical_url TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    page_type TEXT NOT NULL DEFAULT '',
    identifiers_json TEXT NOT NULL DEFAULT '{}',
    extraction_method TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    fetch_status TEXT NOT NULL
);
"""

_NEWSLETTER_ENTRY_COLUMNS = {
    "resolution_status": "TEXT NOT NULL DEFAULT 'pending'",
    "resolution_error": "TEXT",
    "resolved_at": "TEXT",
}


def connect_library(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_library(connection: sqlite3.Connection, *, migrated_at: str) -> None:
    with connection:
        connection.executescript(_SCHEMA)
        row = connection.execute(
            "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
        ).fetchone()
        if row is not None and row["schema_version"] != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported Research Library schema {row['schema_version']}; "
                f"expected {SCHEMA_VERSION}"
            )
        connection.execute(
            "INSERT OR IGNORE INTO schema_metadata(singleton, schema_version, migrated_at) "
            "VALUES (1, ?, ?)",
            (SCHEMA_VERSION, migrated_at),
        )
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(newsletter_entries)")
        }
        for name, definition in _NEWSLETTER_ENTRY_COLUMNS.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE newsletter_entries ADD COLUMN {name} {definition}"
                )
