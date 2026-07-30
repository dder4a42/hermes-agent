"""SQLite connection and schema management for personal English learning."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = "5"


KNOWLEDGE_EVIDENCE_TABLE = """
CREATE TABLE knowledge_evidence (
    id TEXT PRIMARY KEY,
    sense_id TEXT NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
    dimension TEXT NOT NULL CHECK (dimension IN ('recognition', 'recall', 'production')),
    value REAL NOT NULL CHECK (value BETWEEN 0.0 AND 1.0),
    evidence_type TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (dimension, evidence_type, source_event_id)
)
"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS word_senses (
    id TEXT PRIMARY KEY,
    lemma TEXT NOT NULL,
    normalized_lemma TEXT NOT NULL,
    part_of_speech TEXT NOT NULL,
    definition_en TEXT NOT NULL,
    definition_zh TEXT,
    frequency_rank INTEGER CHECK (frequency_rank IS NULL OR frequency_rank > 0),
    source TEXT NOT NULL,
    source_sense_id TEXT NOT NULL,
    identity_derived INTEGER NOT NULL DEFAULT 0 CHECK (identity_derived IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source, source_sense_id)
);

CREATE INDEX IF NOT EXISTS idx_word_senses_frequency
    ON word_senses(frequency_rank, normalized_lemma);

CREATE TABLE IF NOT EXISTS word_pronunciations (
    id TEXT PRIMARY KEY,
    normalized_form TEXT NOT NULL,
    display_form TEXT NOT NULL,
    part_of_speech TEXT,
    dialect TEXT NOT NULL CHECK (dialect IN ('en-US', 'en-GB', 'other')),
    ipa TEXT,
    respelling TEXT,
    arpabet TEXT,
    stress_pattern TEXT,
    variant_rank INTEGER NOT NULL DEFAULT 1 CHECK (variant_rank > 0),
    source TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_license TEXT NOT NULL,
    source_entry_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source, source_entry_id),
    CHECK (ipa IS NOT NULL OR respelling IS NOT NULL OR arpabet IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_word_pronunciations_lookup
    ON word_pronunciations(normalized_form, part_of_speech, dialect, variant_rank);

CREATE TABLE IF NOT EXISTS vocabulary_collections (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('general', 'academic', 'custom')),
    source TEXT NOT NULL,
    version TEXT NOT NULL,
    license TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS word_sense_collections (
    sense_id TEXT NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
    collection_id TEXT NOT NULL REFERENCES vocabulary_collections(id) ON DELETE CASCADE,
    priority_rank INTEGER NOT NULL CHECK (priority_rank > 0),
    sense_rank INTEGER NOT NULL DEFAULT 1 CHECK (sense_rank > 0),
    source_lemma TEXT NOT NULL,
    PRIMARY KEY (sense_id, collection_id)
);

CREATE TABLE IF NOT EXISTS user_knowledge_states (
    sense_id TEXT PRIMARY KEY REFERENCES word_senses(id) ON DELETE CASCADE,
    recognition_score REAL NOT NULL DEFAULT 0.0
        CHECK (recognition_score BETWEEN 0.0 AND 1.0),
    recall_score REAL NOT NULL DEFAULT 0.0
        CHECK (recall_score BETWEEN 0.0 AND 1.0),
    production_score REAL NOT NULL DEFAULT 0.0
        CHECK (production_score BETWEEN 0.0 AND 1.0),
    recognition_evidence_count INTEGER NOT NULL DEFAULT 0,
    recall_evidence_count INTEGER NOT NULL DEFAULT 0,
    production_evidence_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assessment_events (
    id TEXT PRIMARY KEY,
    sense_id TEXT NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
    response TEXT NOT NULL CHECK (response IN ('known', 'unsure', 'unknown')),
    frequency_band TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assessment_events_sense
    ON assessment_events(sense_id, created_at);

CREATE TABLE IF NOT EXISTS knowledge_evidence (
    id TEXT PRIMARY KEY,
    sense_id TEXT NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
    dimension TEXT NOT NULL CHECK (dimension IN ('recognition', 'recall', 'production')),
    value REAL NOT NULL CHECK (value BETWEEN 0.0 AND 1.0),
    evidence_type TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (dimension, evidence_type, source_event_id)
);

CREATE TABLE IF NOT EXISTS review_cards (
    id TEXT PRIMARY KEY,
    sense_id TEXT NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
    card_type TEXT NOT NULL CHECK (card_type IN ('recognition', 'recall')),
    state TEXT NOT NULL DEFAULT 'new'
        CHECK (state IN ('new', 'learning', 'review', 'suspended')),
    step INTEGER NOT NULL DEFAULT 0 CHECK (step >= 0),
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (sense_id, card_type)
);

CREATE INDEX IF NOT EXISTS idx_review_cards_due
    ON review_cards(state, due_at);

CREATE TABLE IF NOT EXISTS review_events (
    id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL REFERENCES review_cards(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL UNIQUE,
    rating TEXT NOT NULL CHECK (rating IN ('again', 'hard', 'good', 'easy')),
    response_time_ms INTEGER CHECK (response_time_ms IS NULL OR response_time_ms >= 0),
    hint_count INTEGER NOT NULL DEFAULT 0 CHECK (hint_count >= 0),
    answer_text TEXT,
    reviewed_at TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    next_due_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_events_card
    ON review_events(card_id, reviewed_at);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source, content_hash)
);

CREATE TABLE IF NOT EXISTS document_tokens (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    token_index INTEGER NOT NULL CHECK (token_index >= 0),
    surface TEXT NOT NULL,
    normalized TEXT NOT NULL,
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    match_status TEXT NOT NULL
        CHECK (match_status IN ('unmatched', 'unique', 'ambiguous')),
    UNIQUE (document_id, token_index)
);

CREATE INDEX IF NOT EXISTS idx_document_tokens_document
    ON document_tokens(document_id, token_index);

CREATE TABLE IF NOT EXISTS document_token_senses (
    token_id TEXT NOT NULL REFERENCES document_tokens(id) ON DELETE CASCADE,
    sense_id TEXT NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
    matched_lemma TEXT NOT NULL,
    PRIMARY KEY (token_id, sense_id)
);

CREATE TABLE IF NOT EXISTS reading_targets (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    sense_id TEXT NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'accepted', 'rejected')),
    priority_score REAL NOT NULL,
    selection_reason TEXT NOT NULL,
    decided_at TEXT,
    PRIMARY KEY (document_id, sense_id)
);

CREATE TABLE IF NOT EXISTS encounters (
    id TEXT PRIMARY KEY,
    sense_id TEXT NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    token_id TEXT NOT NULL REFERENCES document_tokens(id) ON DELETE CASCADE,
    encounter_type TEXT NOT NULL CHECK (encounter_type IN ('reading')),
    created_at TEXT NOT NULL,
    UNIQUE (sense_id, document_id, token_id, encounter_type)
);

CREATE INDEX IF NOT EXISTS idx_encounters_sense
    ON encounters(sense_id, created_at);

CREATE TABLE IF NOT EXISTS production_exercises (
    id TEXT PRIMARY KEY,
    sense_id TEXT NOT NULL REFERENCES word_senses(id) ON DELETE CASCADE,
    document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
    source_token_id TEXT REFERENCES document_tokens(id) ON DELETE SET NULL,
    exercise_type TEXT NOT NULL CHECK (exercise_type IN ('cloze', 'sentence')),
    prompt TEXT NOT NULL,
    expected_answer TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (sense_id, document_id, source_token_id, exercise_type)
);

CREATE INDEX IF NOT EXISTS idx_production_exercises_document
    ON production_exercises(document_id, exercise_type);

CREATE TABLE IF NOT EXISTS production_attempts (
    id TEXT PRIMARY KEY,
    exercise_id TEXT NOT NULL REFERENCES production_exercises(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL UNIQUE,
    answer_text TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('correct', 'partial', 'incorrect')),
    feedback TEXT,
    revision_of_attempt_id TEXT REFERENCES production_attempts(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_production_attempts_exercise
    ON production_attempts(exercise_id, created_at);
"""


class LearningDatabase:
    """Own a profile-local SQLite database with explicit safety pragmas."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_v3(connection)
            self._migrate_v4(connection)
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (SCHEMA_VERSION,),
            )

    @staticmethod
    def _migrate_v3(connection: sqlite3.Connection) -> None:
        """Add production projections while preserving v1/v2 event history."""

        state_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(user_knowledge_states)"
            ).fetchall()
        }
        if "production_score" not in state_columns:
            connection.execute(
                """
                ALTER TABLE user_knowledge_states
                ADD COLUMN production_score REAL NOT NULL DEFAULT 0.0
                    CHECK (production_score BETWEEN 0.0 AND 1.0)
                """
            )
        if "production_evidence_count" not in state_columns:
            connection.execute(
                """
                ALTER TABLE user_knowledge_states
                ADD COLUMN production_evidence_count INTEGER NOT NULL DEFAULT 0
                """
            )

        evidence_sql_row = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'knowledge_evidence'
            """
        ).fetchone()
        evidence_sql = evidence_sql_row["sql"] if evidence_sql_row else ""
        if "'production'" in evidence_sql:
            return
        connection.execute(
            "ALTER TABLE knowledge_evidence RENAME TO knowledge_evidence_v2"
        )
        connection.execute(KNOWLEDGE_EVIDENCE_TABLE)
        connection.execute(
            """
            INSERT INTO knowledge_evidence(
                id, sense_id, dimension, value, evidence_type, source_event_id, created_at
            )
            SELECT id, sense_id, dimension, value, evidence_type, source_event_id, created_at
            FROM knowledge_evidence_v2
            """
        )
        connection.execute("DROP TABLE knowledge_evidence_v2")

    @staticmethod
    def _migrate_v4(connection: sqlite3.Connection) -> None:
        """Add deterministic within-lemma collection ordering."""

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(word_sense_collections)"
            ).fetchall()
        }
        if "sense_rank" not in columns:
            connection.execute(
                """
                ALTER TABLE word_sense_collections
                ADD COLUMN sense_rank INTEGER NOT NULL DEFAULT 1
                    CHECK (sense_rank > 0)
                """
            )
        index = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index' AND name = 'idx_word_sense_collections_rank'
            """
        ).fetchone()
        if not index or "sense_rank" not in (index["sql"] or ""):
            connection.execute(
                "DROP INDEX IF EXISTS idx_word_sense_collections_rank"
            )
            connection.execute(
                """
                CREATE INDEX idx_word_sense_collections_rank
                ON word_sense_collections(
                    collection_id, priority_rank, sense_rank, sense_id
                )
                """
            )
