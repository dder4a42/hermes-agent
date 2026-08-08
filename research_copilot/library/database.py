"""Connection and schema management for the profile-scoped Research Library."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 8

WORKFLOW_STATES = (
    "discovered",
    "shortlisted",
    "reading",
    "synthesized",
    "rejected",
    "archived",
)

ANALYSIS_STATES = (
    "none",
    "triaged",
    "deep_researched",
    "synthesized",
)

LEARNING_STATES = (
    "unseen",
    "saved",
    "reading",
    "read",
    "skipped",
)

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
CREATE TABLE IF NOT EXISTS source_runtime_state (
    source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    provider_state_json TEXT NOT NULL DEFAULT '{}',
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    cooldown_until TEXT,
    last_error_code TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_items (
    id TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    authors_json TEXT NOT NULL DEFAULT '[]',
    published_at TEXT,
    first_discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    workflow_state TEXT NOT NULL DEFAULT 'discovered'
        CHECK (workflow_state IN ('discovered', 'shortlisted', 'reading', 'synthesized', 'rejected', 'archived')),
    agent_analysis_status TEXT NOT NULL DEFAULT 'none'
        CHECK (agent_analysis_status IN ('none', 'triaged', 'deep_researched', 'synthesized')),
    agent_analysis_updated_at TEXT,
    user_learning_status TEXT NOT NULL DEFAULT 'unseen'
        CHECK (user_learning_status IN ('unseen', 'saved', 'reading', 'read', 'skipped')),
    user_learning_updated_at TEXT,
    starred INTEGER NOT NULL DEFAULT 0 CHECK (starred IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS item_identifiers (
    item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
    scheme TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (scheme, value)
);
CREATE TABLE IF NOT EXISTS item_merge_evidence (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    method TEXT NOT NULL,
    incoming_canonical_key TEXT NOT NULL,
    evidence_json TEXT NOT NULL
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
    item_id TEXT NOT NULL REFERENCES research_items(id),
    recommended_at TEXT NOT NULL,
    score REAL NOT NULL CHECK (score >= 0 AND score <= 1),
    score_breakdown_json TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_recommendations_item_time
    ON recommendations(item_id, recommended_at DESC);
CREATE TABLE IF NOT EXISTS recommendation_delivery_outbox (
    id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL UNIQUE REFERENCES recommendations(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempt_at TEXT,
    delivered_at TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_recommendation_outbox_pending
    ON recommendation_delivery_outbox(status, created_at);
CREATE TABLE IF NOT EXISTS deep_research_artifacts (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
    schema_version INTEGER NOT NULL,
    generated_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    research_question TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    artifact_json TEXT NOT NULL,
    producer_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_deep_research_item_time
    ON deep_research_artifacts(item_id, generated_at DESC);
CREATE TABLE IF NOT EXISTS deep_research_delivery_outbox (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL UNIQUE REFERENCES deep_research_artifacts(id) ON DELETE CASCADE,
    item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    payload_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempt_at TEXT,
    delivered_at TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_deep_research_outbox_pending
    ON deep_research_delivery_outbox(status, created_at);
CREATE TABLE IF NOT EXISTS scout_delivery_outbox (
    id TEXT PRIMARY KEY,
    artifact_path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    payload_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempt_at TEXT,
    delivered_at TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scout_outbox_pending
    ON scout_delivery_outbox(status, created_at);
CREATE TABLE IF NOT EXISTS feedback_events (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES research_items(id),
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS evidence_records (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    agenda_id TEXT NOT NULL,
    belief_id TEXT,
    prompt_id TEXT,
    relation TEXT NOT NULL CHECK (relation IN ('supports', 'challenges', 'contextualizes')),
    claim_type TEXT NOT NULL CHECK (claim_type IN ('source_claim', 'agent_inference', 'personal_take')),
    strength REAL NOT NULL CHECK (strength >= 0 AND strength <= 1),
    source_quality TEXT NOT NULL CHECK (source_quality IN ('primary', 'official', 'secondary', 'community', 'unknown')),
    claim TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'accepted', 'rejected')),
    reviewed_at TEXT,
    review_note TEXT NOT NULL DEFAULT '',
    profile_revision TEXT NOT NULL,
    CHECK (belief_id IS NOT NULL OR prompt_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_evidence_item_time
    ON evidence_records(item_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_belief_review
    ON evidence_records(belief_id, review_status);
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


def _migrate_v1_to_v2(connection: sqlite3.Connection, *, migrated_at: str) -> None:
    """Separate item workflow state from recommendation/feedback events."""
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(research_items)")
    }
    if "workflow_state" not in columns:
        connection.execute("ALTER TABLE research_items RENAME COLUMN status TO workflow_state")
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(research_items)")
    }
    if "starred" not in columns:
        connection.execute(
            "ALTER TABLE research_items ADD COLUMN starred INTEGER NOT NULL DEFAULT 0 "
            "CHECK (starred IN (0, 1))"
        )
    connection.execute(
        """
        UPDATE research_items
        SET workflow_state = CASE workflow_state
            WHEN 'saved' THEN 'shortlisted'
            WHEN 'read' THEN 'reading'
            WHEN 'skipped' THEN 'rejected'
            WHEN 'archived' THEN 'archived'
            ELSE 'discovered'
        END
        """
    )
    connection.execute(
        """
        CREATE TABLE recommendations_v2 (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL REFERENCES research_items(id),
            recommended_at TEXT NOT NULL,
            score REAL NOT NULL CHECK (score >= 0 AND score <= 1),
            score_breakdown_json TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        """
        INSERT INTO recommendations_v2(
            id, item_id, recommended_at, score, score_breakdown_json, rationale
        )
        SELECT id, item_id, recommended_at, score, score_breakdown_json, rationale
        FROM recommendations
        """
    )
    connection.execute("DROP TABLE recommendations")
    connection.execute("ALTER TABLE recommendations_v2 RENAME TO recommendations")
    connection.execute(
        "CREATE INDEX idx_recommendations_item_time "
        "ON recommendations(item_id, recommended_at DESC)"
    )
    connection.execute(
        "UPDATE schema_metadata SET schema_version = ?, migrated_at = ? WHERE singleton = 1",
        (2, migrated_at),
    )


def _migrate_v2_to_v3(connection: sqlite3.Connection, *, migrated_at: str) -> None:
    """Add durable per-source cursors and failure cooldown state."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_runtime_state (
            source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
            provider_state_json TEXT NOT NULL DEFAULT '{}',
            consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
            cooldown_until TEXT,
            last_error_code TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "UPDATE schema_metadata SET schema_version = 3, migrated_at = ? WHERE singleton = 1",
        (migrated_at,),
    )


def _migrate_v3_to_v4(connection: sqlite3.Connection, *, migrated_at: str) -> None:
    """Add reviewable evidence records without mutating profile beliefs."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS evidence_records (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            agenda_id TEXT NOT NULL,
            belief_id TEXT,
            prompt_id TEXT,
            relation TEXT NOT NULL CHECK (relation IN ('supports', 'challenges', 'contextualizes')),
            claim_type TEXT NOT NULL CHECK (claim_type IN ('source_claim', 'agent_inference', 'personal_take')),
            strength REAL NOT NULL CHECK (strength >= 0 AND strength <= 1),
            source_quality TEXT NOT NULL CHECK (source_quality IN ('primary', 'official', 'secondary', 'community', 'unknown')),
            claim TEXT NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            review_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (review_status IN ('pending', 'accepted', 'rejected')),
            reviewed_at TEXT,
            review_note TEXT NOT NULL DEFAULT '',
            profile_revision TEXT NOT NULL,
            CHECK (belief_id IS NOT NULL OR prompt_id IS NOT NULL)
        );
        CREATE INDEX IF NOT EXISTS idx_evidence_item_time
            ON evidence_records(item_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_evidence_belief_review
            ON evidence_records(belief_id, review_status);
        """
    )
    connection.execute(
        "UPDATE schema_metadata SET schema_version = 4, migrated_at = ? WHERE singleton = 1",
        (migrated_at,),
    )


def _migrate_v4_to_v5(connection: sqlite3.Connection, *, migrated_at: str) -> None:
    """Separate machine analysis progress from the reader's learning progress."""
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(research_items)")
    }
    additions = {
        "agent_analysis_status": (
            "TEXT NOT NULL DEFAULT 'none' CHECK (agent_analysis_status IN "
            "('none', 'triaged', 'deep_researched', 'synthesized'))"
        ),
        "agent_analysis_updated_at": "TEXT",
        "user_learning_status": (
            "TEXT NOT NULL DEFAULT 'unseen' CHECK (user_learning_status IN "
            "('unseen', 'saved', 'reading', 'read', 'skipped'))"
        ),
        "user_learning_updated_at": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE research_items ADD COLUMN {name} {definition}"
            )

    # Old workflow values mixed human feedback with machine synthesis.  Only
    # synthesized is safe to interpret as an analysis result; the remaining
    # values are mapped onto the reader state without inventing agent work.
    connection.execute(
        """
        UPDATE research_items
        SET agent_analysis_status = CASE workflow_state
                WHEN 'synthesized' THEN 'synthesized'
                ELSE 'none'
            END,
            agent_analysis_updated_at = CASE workflow_state
                WHEN 'synthesized' THEN ? ELSE NULL
            END,
            user_learning_status = CASE workflow_state
                WHEN 'shortlisted' THEN 'saved'
                WHEN 'reading' THEN 'read'
                WHEN 'synthesized' THEN 'read'
                WHEN 'rejected' THEN 'skipped'
                ELSE 'unseen'
            END,
            user_learning_updated_at = CASE
                WHEN workflow_state IN ('shortlisted', 'reading', 'synthesized', 'rejected')
                THEN ? ELSE NULL
            END
        """,
        (migrated_at, migrated_at),
    )
    connection.execute(
        "UPDATE schema_metadata SET schema_version = 5, migrated_at = ? WHERE singleton = 1",
        (migrated_at,),
    )


def _ensure_recommendation_outbox(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS recommendation_delivery_outbox (
            id TEXT PRIMARY KEY,
            recommendation_id TEXT NOT NULL UNIQUE REFERENCES recommendations(id) ON DELETE CASCADE,
            item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            last_attempt_at TEXT,
            delivered_at TEXT,
            last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_recommendation_outbox_pending
            ON recommendation_delivery_outbox(status, created_at);
        DROP TRIGGER IF EXISTS recommendations_enqueue_delivery;
        """
    )


def _ensure_deep_research_artifacts(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS deep_research_artifacts (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
            schema_version INTEGER NOT NULL,
            generated_at TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            research_question TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            artifact_json TEXT NOT NULL,
            producer_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_deep_research_item_time
            ON deep_research_artifacts(item_id, generated_at DESC);
        """
    )


def _ensure_deep_research_outbox(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS deep_research_delivery_outbox (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL UNIQUE REFERENCES deep_research_artifacts(id) ON DELETE CASCADE,
            item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            payload_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            last_attempt_at TEXT,
            delivered_at TEXT,
            last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_deep_research_outbox_pending
            ON deep_research_delivery_outbox(status, created_at);
        """
    )


def _migrate_v5_to_v6(connection: sqlite3.Connection, *, migrated_at: str) -> None:
    """Add acknowledged delivery and structured deep-research artifacts."""
    _ensure_recommendation_outbox(connection)
    _ensure_deep_research_artifacts(connection)
    connection.execute(
        "UPDATE schema_metadata SET schema_version = 6, migrated_at = ? WHERE singleton = 1",
        (migrated_at,),
    )


def _migrate_v6_to_v7(connection: sqlite3.Connection, *, migrated_at: str) -> None:
    """Add acknowledged delivery for generated deep-research learning cards."""
    _ensure_deep_research_outbox(connection)
    connection.execute(
        "UPDATE schema_metadata SET schema_version = 7, migrated_at = ? WHERE singleton = 1",
        (migrated_at,),
    )


def _ensure_scout_outbox(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS scout_delivery_outbox (
            id TEXT PRIMARY KEY,
            artifact_path TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            payload_text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            last_attempt_at TEXT,
            delivered_at TEXT,
            last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_scout_outbox_pending
            ON scout_delivery_outbox(status, created_at);
        """
    )


def _migrate_v7_to_v8(connection: sqlite3.Connection, *, migrated_at: str) -> None:
    """Add acknowledged delivery for weekly Scout reports."""
    _ensure_scout_outbox(connection)
    connection.execute(
        "UPDATE schema_metadata SET schema_version = 8, migrated_at = ? WHERE singleton = 1",
        (migrated_at,),
    )


def _ensure_workflow_guards(connection: sqlite3.Connection) -> None:
    allowed = ", ".join(f"'{value}'" for value in WORKFLOW_STATES)
    connection.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS research_items_workflow_insert
        BEFORE INSERT ON research_items
        WHEN NEW.workflow_state NOT IN ({allowed})
        BEGIN
            SELECT RAISE(ABORT, 'invalid research item workflow_state');
        END
        """
    )
    connection.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS research_items_workflow_update
        BEFORE UPDATE OF workflow_state ON research_items
        WHEN NEW.workflow_state NOT IN ({allowed})
        BEGIN
            SELECT RAISE(ABORT, 'invalid research item workflow_state');
        END
        """
    )


def _ensure_identity_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(research_items)")
    }
    if "normalized_title" not in columns:
        connection.execute(
            "ALTER TABLE research_items ADD COLUMN normalized_title TEXT NOT NULL DEFAULT ''"
        )
    from .identity import normalize_title

    rows = connection.execute(
        "SELECT id,title FROM research_items WHERE normalized_title=''"
    ).fetchall()
    connection.executemany(
        "UPDATE research_items SET normalized_title=? WHERE id=?",
        ((normalize_title(row["title"]), row["id"]) for row in rows),
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_items_normalized_title "
        "ON research_items(normalized_title)"
    )


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
        if row is not None and row["schema_version"] == 1:
            _migrate_v1_to_v2(connection, migrated_at=migrated_at)
            row = connection.execute(
                "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
            ).fetchone()
        if row is not None and row["schema_version"] == 2:
            _migrate_v2_to_v3(connection, migrated_at=migrated_at)
            row = connection.execute(
                "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
            ).fetchone()
        if row is not None and row["schema_version"] == 3:
            _migrate_v3_to_v4(connection, migrated_at=migrated_at)
            row = connection.execute(
                "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
            ).fetchone()
        if row is not None and row["schema_version"] == 4:
            _migrate_v4_to_v5(connection, migrated_at=migrated_at)
            row = connection.execute(
                "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
            ).fetchone()
        if row is not None and row["schema_version"] == 5:
            _migrate_v5_to_v6(connection, migrated_at=migrated_at)
            row = connection.execute(
                "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
            ).fetchone()
        if row is not None and row["schema_version"] == 6:
            _migrate_v6_to_v7(connection, migrated_at=migrated_at)
            row = connection.execute(
                "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
            ).fetchone()
        if row is not None and row["schema_version"] == 7:
            _migrate_v7_to_v8(connection, migrated_at=migrated_at)
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
        _ensure_workflow_guards(connection)
        _ensure_recommendation_outbox(connection)
        _ensure_deep_research_artifacts(connection)
        _ensure_deep_research_outbox(connection)
        _ensure_scout_outbox(connection)
        _ensure_identity_columns(connection)
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(newsletter_entries)")
        }
        for name, definition in _NEWSLETTER_ENTRY_COLUMNS.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE newsletter_entries ADD COLUMN {name} {definition}"
                )
