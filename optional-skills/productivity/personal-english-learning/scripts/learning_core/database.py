"""SQLite connection and schema management for personal English learning."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = "1"


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

CREATE TABLE IF NOT EXISTS user_knowledge_states (
    sense_id TEXT PRIMARY KEY REFERENCES word_senses(id) ON DELETE CASCADE,
    recognition_score REAL NOT NULL DEFAULT 0.0
        CHECK (recognition_score BETWEEN 0.0 AND 1.0),
    recall_score REAL NOT NULL DEFAULT 0.0
        CHECK (recall_score BETWEEN 0.0 AND 1.0),
    recognition_evidence_count INTEGER NOT NULL DEFAULT 0,
    recall_evidence_count INTEGER NOT NULL DEFAULT 0,
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
    dimension TEXT NOT NULL CHECK (dimension IN ('recognition', 'recall')),
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
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (SCHEMA_VERSION,),
            )
