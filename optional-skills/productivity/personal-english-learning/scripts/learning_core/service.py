"""Deterministic vocabulary assessment and review workflows."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .database import LearningDatabase
from .pronunciation import PronunciationService


ASSESSMENT_VALUES = {"known": 0.9, "unsure": 0.45, "unknown": 0.05}
REVIEW_VALUES = {"again": 0.0, "hard": 0.35, "good": 0.75, "easy": 0.95}
DEFAULT_BANDS = ((1, 1000), (1001, 2000), (2001, 3000), (3001, 5000))
RATINGS = frozenset(REVIEW_VALUES)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def normalize_lemma(value: str) -> str:
    return " ".join(value.casefold().strip().split())


@dataclass(frozen=True)
class VocabularyCollection:
    id: str
    title: str
    kind: str
    source: str
    version: str
    license: str
    rank: int
    source_lemma: str
    sense_rank: int = 1

    def __post_init__(self) -> None:
        if not all(
            value and value.strip()
            for value in (
                self.id,
                self.title,
                self.source,
                self.version,
                self.license,
                self.source_lemma,
            )
        ):
            raise ValueError("collection metadata fields are required")
        if self.kind not in {"general", "academic", "custom"}:
            raise ValueError("collection kind must be general, academic, or custom")
        if self.rank <= 0:
            raise ValueError("collection rank must be positive")
        if self.sense_rank <= 0:
            raise ValueError("collection sense_rank must be positive")

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "VocabularyCollection":
        if not isinstance(value, dict):
            raise ValueError("collection must be a JSON object")
        return cls(
            id=str(value.get("id", "")).strip(),
            title=str(value.get("title", "")).strip(),
            kind=str(value.get("kind", "")).strip(),
            source=str(value.get("source", "")).strip(),
            version=str(value.get("version", "")).strip(),
            license=str(value.get("license", "")).strip(),
            rank=int(value.get("rank", 0)),
            source_lemma=str(value.get("source_lemma", "")).strip(),
            sense_rank=int(value.get("sense_rank", 1)),
        )


@dataclass(frozen=True)
class VocabularyEntry:
    lemma: str
    part_of_speech: str
    definition_en: str
    definition_zh: str | None
    frequency_rank: int | None
    source: str
    source_sense_id: str | None = None
    collection: VocabularyCollection | None = None

    def __post_init__(self) -> None:
        if not all(
            value and value.strip()
            for value in (self.lemma, self.part_of_speech, self.definition_en, self.source)
        ):
            raise ValueError(
                "lemma, part_of_speech, definition_en, and source are required"
            )
        if self.frequency_rank is not None and self.frequency_rank <= 0:
            raise ValueError("frequency_rank must be positive")

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "VocabularyEntry":
        rank = value.get("frequency_rank")
        if rank is not None:
            rank = int(rank)
            if rank <= 0:
                raise ValueError("frequency_rank must be positive")
        entry = cls(
            lemma=str(value.get("lemma", "")).strip(),
            part_of_speech=str(value.get("part_of_speech", "")).strip(),
            definition_en=str(value.get("definition_en", "")).strip(),
            definition_zh=(
                str(value["definition_zh"]).strip()
                if value.get("definition_zh") is not None
                else None
            ),
            frequency_rank=rank,
            source=str(value.get("source", "")).strip(),
            source_sense_id=(
                str(value["source_sense_id"]).strip()
                if value.get("source_sense_id")
                else None
            ),
            collection=(
                VocabularyCollection.from_mapping(value["collection"])
                if value.get("collection") is not None
                else None
            ),
        )
        return entry


class LearningService:
    def __init__(self, database: LearningDatabase):
        self.database = database
        self.database.initialize()

    @staticmethod
    def _identity(entry: VocabularyEntry) -> tuple[str, bool]:
        if entry.source_sense_id:
            return entry.source_sense_id, False
        material = "\x1f".join(
            (
                normalize_lemma(entry.lemma),
                entry.part_of_speech.casefold(),
                entry.definition_en.casefold(),
            )
        )
        return "derived:" + hashlib.sha256(material.encode("utf-8")).hexdigest(), True

    def upsert_vocabulary(self, entry: VocabularyEntry, *, now: datetime | None = None) -> dict:
        now = now or utc_now()
        source_sense_id, derived = self._identity(entry)
        sense_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{entry.source}:{source_sense_id}")
        )
        with self.database.connect() as connection:
            existed = connection.execute(
                "SELECT 1 FROM word_senses WHERE source = ? AND source_sense_id = ?",
                (entry.source, source_sense_id),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO word_senses(
                    id, lemma, normalized_lemma, part_of_speech, definition_en,
                    definition_zh, frequency_rank, source, source_sense_id,
                    identity_derived, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_sense_id) DO UPDATE SET
                    lemma = excluded.lemma,
                    normalized_lemma = excluded.normalized_lemma,
                    part_of_speech = excluded.part_of_speech,
                    definition_en = excluded.definition_en,
                    definition_zh = COALESCE(
                        excluded.definition_zh, word_senses.definition_zh
                    ),
                    frequency_rank = COALESCE(
                        excluded.frequency_rank, word_senses.frequency_rank
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    sense_id,
                    entry.lemma,
                    normalize_lemma(entry.lemma),
                    entry.part_of_speech,
                    entry.definition_en,
                    entry.definition_zh,
                    entry.frequency_rank,
                    entry.source,
                    source_sense_id,
                    int(derived),
                    iso(now),
                    iso(now),
                ),
            )
            if entry.collection is not None:
                collection = entry.collection
                connection.execute(
                    """
                    INSERT INTO vocabulary_collections(
                        id, title, kind, source, version, license, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        kind = excluded.kind,
                        source = excluded.source,
                        version = excluded.version,
                        license = excluded.license
                    """,
                    (
                        collection.id,
                        collection.title,
                        collection.kind,
                        collection.source,
                        collection.version,
                        collection.license,
                        iso(now),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO word_sense_collections(
                        sense_id, collection_id, priority_rank, sense_rank,
                        source_lemma
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(sense_id, collection_id) DO UPDATE SET
                        priority_rank = excluded.priority_rank,
                        sense_rank = excluded.sense_rank,
                        source_lemma = excluded.source_lemma
                    """,
                    (
                        sense_id,
                        collection.id,
                        collection.rank,
                        collection.sense_rank,
                        collection.source_lemma,
                    ),
                )
        return {
            "sense_id": sense_id,
            "created": existed is None,
            "identity_derived": derived,
            "collection_id": (
                entry.collection.id if entry.collection is not None else None
            ),
        }

    def import_jsonl(self, path: str | Path) -> dict:
        imported = 0
        updated = 0
        derived = 0
        errors: list[dict[str, Any]] = []
        with Path(path).expanduser().open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, dict):
                        raise ValueError("line must contain a JSON object")
                    result = self.upsert_vocabulary(VocabularyEntry.from_mapping(raw))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    errors.append({"line": line_number, "error": str(exc)})
                    continue
                imported += int(result["created"])
                updated += int(not result["created"])
                derived += int(result["identity_derived"])
        return {
            "created": imported,
            "updated": updated,
            "derived_identities": derived,
            "errors": errors,
        }

    def assessment_sample(
        self,
        *,
        per_band: int = 10,
        seed: int = 0,
        bands: Iterable[tuple[int, int]] = DEFAULT_BANDS,
        collection_id: str | None = None,
    ) -> dict:
        if per_band <= 0:
            raise ValueError("per_band must be positive")
        generator = random.Random(seed)
        samples: list[dict] = []
        with self.database.connect() as connection:
            collection_id, collection_selection = self._resolve_collection_id(
                connection, collection_id
            )
            for lower, upper in bands:
                if collection_id:
                    rows = connection.execute(
                        """
                        SELECT ws.*, wsc.collection_id,
                               wsc.priority_rank AS collection_rank,
                               wsc.sense_rank AS collection_sense_rank
                        FROM word_senses ws
                        JOIN word_sense_collections wsc ON wsc.sense_id = ws.id
                        WHERE wsc.collection_id = ?
                          AND wsc.priority_rank BETWEEN ? AND ?
                          AND wsc.sense_rank = 1
                          AND NOT EXISTS (
                              SELECT 1 FROM assessment_events ae
                              WHERE ae.sense_id = ws.id
                          )
                        ORDER BY wsc.priority_rank, ws.normalized_lemma, ws.id
                        """,
                        (collection_id, lower, upper),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT ws.* FROM word_senses ws
                        WHERE ws.frequency_rank BETWEEN ? AND ?
                          AND NOT EXISTS (
                              SELECT 1 FROM assessment_events ae
                              WHERE ae.sense_id = ws.id
                          )
                        ORDER BY ws.frequency_rank, ws.normalized_lemma, ws.id
                        """,
                        (lower, upper),
                    ).fetchall()
                chosen = generator.sample(rows, min(per_band, len(rows)))
                for row in chosen:
                    item = self._sense_dict(row)
                    item["frequency_band"] = f"{lower}-{upper}"
                    samples.append(item)
        self._attach_pronunciations(samples)
        return {
            "count": len(samples),
            "collection_id": collection_id,
            "collection_selection": collection_selection,
            "items": samples,
        }

    def record_assessment(
        self,
        sense_id: str,
        response: str,
        frequency_band: str,
        *,
        event_id: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        if response not in ASSESSMENT_VALUES:
            raise ValueError("response must be known, unsure, or unknown")
        now = now or utc_now()
        event_id = event_id or str(uuid.uuid4())
        with self.database.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM word_senses WHERE id = ?", (sense_id,)
            ).fetchone():
                raise ValueError(f"unknown sense_id: {sense_id}")
            existing = connection.execute(
                "SELECT * FROM assessment_events WHERE id = ?", (event_id,)
            ).fetchone()
            if existing:
                if (
                    existing["sense_id"] != sense_id
                    or existing["response"] != response
                    or existing["frequency_band"] != frequency_band
                ):
                    raise ValueError(
                        "assessment event_id was already used for a different request"
                    )
                return {"event_id": event_id, "duplicate": True}
            connection.execute(
                """
                INSERT INTO assessment_events(id, sense_id, response, frequency_band, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, sense_id, response, frequency_band, iso(now)),
            )
            self._add_evidence(
                connection,
                sense_id=sense_id,
                dimension="recognition",
                value=ASSESSMENT_VALUES[response],
                evidence_type="assessment",
                source_event_id=event_id,
                now=now,
            )
        return {"event_id": event_id, "duplicate": False}

    def daily_plan(
        self,
        *,
        review_limit: int = 30,
        new_limit: int = 8,
        backlog_reduce_at: int = 30,
        backlog_stop_at: int = 60,
        collection_id: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        if review_limit < 0 or new_limit < 0:
            raise ValueError("review_limit and new_limit cannot be negative")
        if not 0 <= backlog_reduce_at <= backlog_stop_at:
            raise ValueError("backlog thresholds are invalid")
        now = now or utc_now()
        now_text = iso(now)
        with self.database.connect() as connection:
            collection_id, collection_selection = self._resolve_collection_id(
                connection, collection_id
            )
            due_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM review_cards
                    WHERE state != 'suspended' AND due_at <= ?
                    """,
                    (now_text,),
                ).fetchone()[0]
            )
            effective_new_limit = new_limit
            if due_count >= backlog_stop_at:
                effective_new_limit = 0
            elif due_count >= backlog_reduce_at:
                effective_new_limit = min(new_limit, max(1, new_limit // 2))

            due_rows = connection.execute(
                """
                SELECT rc.*, ws.lemma, ws.part_of_speech, ws.definition_en,
                       ws.definition_zh, ws.frequency_rank, ws.source,
                       ws.source_sense_id
                FROM review_cards rc
                JOIN word_senses ws ON ws.id = rc.sense_id
                WHERE rc.state != 'suspended' AND rc.due_at <= ?
                ORDER BY rc.due_at, ws.frequency_rank, ws.normalized_lemma
                LIMIT ?
                """,
                (now_text, review_limit),
            ).fetchall()

            if collection_id:
                candidates = connection.execute(
                    """
                    SELECT ws.*, wsc.collection_id,
                           wsc.priority_rank AS collection_rank,
                           wsc.sense_rank AS collection_sense_rank
                    FROM word_senses ws
                    JOIN word_sense_collections wsc ON wsc.sense_id = ws.id
                    LEFT JOIN user_knowledge_states uks ON uks.sense_id = ws.id
                    WHERE wsc.collection_id = ?
                      AND COALESCE(uks.recognition_score, 0.0) < 0.7
                      AND NOT EXISTS (
                          SELECT 1 FROM review_cards rc WHERE rc.sense_id = ws.id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM word_senses earlier_ws
                          JOIN word_sense_collections earlier_wsc
                            ON earlier_wsc.sense_id = earlier_ws.id
                          LEFT JOIN user_knowledge_states earlier_uks
                            ON earlier_uks.sense_id = earlier_ws.id
                          WHERE earlier_wsc.collection_id = wsc.collection_id
                            AND earlier_ws.normalized_lemma = ws.normalized_lemma
                            AND COALESCE(earlier_uks.recognition_score, 0.0) < 0.7
                            AND NOT EXISTS (
                                SELECT 1 FROM review_cards earlier_rc
                                WHERE earlier_rc.sense_id = earlier_ws.id
                            )
                            AND (
                                earlier_wsc.priority_rank < wsc.priority_rank
                                OR (
                                    earlier_wsc.priority_rank = wsc.priority_rank
                                    AND earlier_wsc.sense_rank < wsc.sense_rank
                                )
                                OR (
                                    earlier_wsc.priority_rank = wsc.priority_rank
                                    AND earlier_wsc.sense_rank = wsc.sense_rank
                                    AND earlier_ws.id < ws.id
                                )
                            )
                      )
                    ORDER BY
                        CASE
                            WHEN COALESCE(uks.recognition_evidence_count, 0) > 0 THEN 0
                            ELSE 1
                        END,
                        COALESCE(uks.recognition_score, 0.0),
                        wsc.priority_rank,
                        wsc.sense_rank,
                        ws.normalized_lemma,
                        ws.id
                    LIMIT ?
                    """,
                    (collection_id, effective_new_limit),
                ).fetchall()
            else:
                candidates = connection.execute(
                    """
                    SELECT ws.* FROM word_senses ws
                    LEFT JOIN user_knowledge_states uks ON uks.sense_id = ws.id
                    WHERE ws.frequency_rank IS NOT NULL
                      AND COALESCE(uks.recognition_score, 0.0) < 0.7
                      AND NOT EXISTS (
                          SELECT 1 FROM review_cards rc WHERE rc.sense_id = ws.id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM word_senses earlier_ws
                          LEFT JOIN user_knowledge_states earlier_uks
                            ON earlier_uks.sense_id = earlier_ws.id
                          WHERE earlier_ws.normalized_lemma = ws.normalized_lemma
                            AND earlier_ws.frequency_rank IS NOT NULL
                            AND COALESCE(earlier_uks.recognition_score, 0.0) < 0.7
                            AND NOT EXISTS (
                                SELECT 1 FROM review_cards earlier_rc
                                WHERE earlier_rc.sense_id = earlier_ws.id
                            )
                            AND (
                                earlier_ws.frequency_rank < ws.frequency_rank
                                OR (
                                    earlier_ws.frequency_rank = ws.frequency_rank
                                    AND earlier_ws.id < ws.id
                                )
                            )
                      )
                    ORDER BY
                        CASE
                            WHEN COALESCE(uks.recognition_evidence_count, 0) > 0 THEN 0
                            ELSE 1
                        END,
                        COALESCE(uks.recognition_score, 0.0),
                        CASE
                            WHEN EXISTS (
                                SELECT 1
                                FROM word_sense_collections ordering_wsc
                                JOIN vocabulary_collections ordering_vc
                                  ON ordering_vc.id = ordering_wsc.collection_id
                                WHERE ordering_wsc.sense_id = ws.id
                                  AND ordering_vc.kind = 'general'
                            ) THEN 0
                            WHEN NOT EXISTS (
                                SELECT 1 FROM word_sense_collections ordering_wsc
                                WHERE ordering_wsc.sense_id = ws.id
                            ) THEN 1
                            ELSE 2
                        END,
                        ws.frequency_rank,
                        ws.normalized_lemma,
                        ws.id
                    LIMIT ?
                    """,
                    (effective_new_limit,),
                ).fetchall()

            new_items = []
            for row in candidates:
                card_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO review_cards(
                        id, sense_id, card_type, state, step, due_at, created_at, updated_at
                    ) VALUES (?, ?, 'recognition', 'new', 0, ?, ?, ?)
                    """,
                    (card_id, row["id"], now_text, now_text, now_text),
                )
                item = self._sense_dict(row)
                item.update({"card_id": card_id, "card_type": "recognition"})
                new_items.append(item)

        review_items = [self._card_dict(row) for row in due_rows]
        self._attach_pronunciations(review_items)
        self._attach_pronunciations(new_items)
        return {
            "generated_at": now_text,
            "due_count": due_count,
            "review_limit": review_limit,
            "requested_new_limit": new_limit,
            "effective_new_limit": effective_new_limit,
            "collection_id": collection_id,
            "collection_selection": collection_selection,
            "reviews": review_items,
            "new_items": new_items,
        }

    def record_review(
        self,
        card_id: str,
        rating: str,
        idempotency_key: str,
        *,
        response_time_ms: int | None = None,
        hint_count: int = 0,
        answer_text: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        if rating not in RATINGS:
            raise ValueError("rating must be again, hard, good, or easy")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if response_time_ms is not None and response_time_ms < 0:
            raise ValueError("response_time_ms cannot be negative")
        if hint_count < 0:
            raise ValueError("hint_count cannot be negative")
        now = now or utc_now()
        with self.database.connect() as connection:
            duplicate = connection.execute(
                "SELECT * FROM review_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                if duplicate["card_id"] != card_id or duplicate["rating"] != rating:
                    raise ValueError(
                        "idempotency_key was already used for a different review"
                    )
                return self._review_result(duplicate, duplicate=True)
            card = connection.execute(
                "SELECT * FROM review_cards WHERE id = ?", (card_id,)
            ).fetchone()
            if not card:
                raise ValueError(f"unknown card_id: {card_id}")
            interval_seconds, next_step, next_state = self._schedule(
                int(card["step"]), rating
            )
            next_due = now + timedelta(seconds=interval_seconds)
            event_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO review_events(
                    id, card_id, idempotency_key, rating, response_time_ms,
                    hint_count, answer_text, reviewed_at, interval_seconds, next_due_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    card_id,
                    idempotency_key,
                    rating,
                    response_time_ms,
                    hint_count,
                    answer_text,
                    iso(now),
                    interval_seconds,
                    iso(next_due),
                ),
            )
            connection.execute(
                """
                UPDATE review_cards
                SET step = ?, state = ?, due_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_step, next_state, iso(next_due), iso(now), card_id),
            )
            self._add_evidence(
                connection,
                sense_id=card["sense_id"],
                dimension=card["card_type"],
                value=REVIEW_VALUES[rating],
                evidence_type="review",
                source_event_id=event_id,
                now=now,
            )
            if card["card_type"] == "recognition":
                self._maybe_unlock_recall_card(connection, card["sense_id"], now)
            event = connection.execute(
                "SELECT * FROM review_events WHERE id = ?", (event_id,)
            ).fetchone()
        return self._review_result(event, duplicate=False)

    def stats(self, *, now: datetime | None = None) -> dict:
        now = now or utc_now()
        with self.database.connect() as connection:
            vocabulary = int(connection.execute("SELECT COUNT(*) FROM word_senses").fetchone()[0])
            pronunciations = int(
                connection.execute("SELECT COUNT(*) FROM word_pronunciations").fetchone()[0]
            )
            assessed = int(
                connection.execute("SELECT COUNT(DISTINCT sense_id) FROM assessment_events").fetchone()[0]
            )
            due = int(
                connection.execute(
                    "SELECT COUNT(*) FROM review_cards WHERE state != 'suspended' AND due_at <= ?",
                    (iso(now),),
                ).fetchone()[0]
            )
            states = connection.execute(
                """
                SELECT
                    COUNT(*) AS tracked,
                    COALESCE(AVG(CASE WHEN recognition_evidence_count > 0
                                      THEN recognition_score END), 0.0) AS recognition,
                    COALESCE(AVG(CASE WHEN recall_evidence_count > 0
                                      THEN recall_score END), 0.0) AS recall,
                    COALESCE(AVG(CASE WHEN production_evidence_count > 0
                                      THEN production_score END), 0.0) AS production
                FROM user_knowledge_states
                """
            ).fetchone()
            collections = connection.execute(
                """
                SELECT vc.id, vc.title, vc.kind, COUNT(wsc.sense_id) AS sense_count
                FROM vocabulary_collections vc
                LEFT JOIN word_sense_collections wsc ON wsc.collection_id = vc.id
                GROUP BY vc.id, vc.title, vc.kind
                ORDER BY vc.kind, vc.id
                """
            ).fetchall()
        return {
            "vocabulary_senses": vocabulary,
            "pronunciations": pronunciations,
            "assessed_senses": assessed,
            "tracked_senses": int(states["tracked"]),
            "due_reviews": due,
            "mean_recognition": round(float(states["recognition"]), 4),
            "mean_recall": round(float(states["recall"]), 4),
            "mean_production": round(float(states["production"]), 4),
            "collections": [dict(row) for row in collections],
        }

    def search_vocabulary(
        self,
        query: str,
        *,
        collection_id: str | None = None,
        limit: int = 50,
    ) -> dict:
        normalized = normalize_lemma(query)
        if not normalized:
            raise ValueError("query is required")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        pattern = f"%{normalized}%"
        with self.database.connect() as connection:
            if collection_id:
                self._resolve_collection_id(connection, collection_id)
                rows = connection.execute(
                    """
                    SELECT ws.*, wsc.collection_id,
                           wsc.priority_rank AS collection_rank,
                           wsc.sense_rank AS collection_sense_rank,
                           COALESCE(uks.recognition_score, 0.0) AS recognition_score,
                           COALESCE(uks.recall_score, 0.0) AS recall_score,
                           COALESCE(uks.production_score, 0.0) AS production_score
                    FROM word_senses ws
                    JOIN word_sense_collections wsc ON wsc.sense_id = ws.id
                    LEFT JOIN user_knowledge_states uks ON uks.sense_id = ws.id
                    WHERE wsc.collection_id = ?
                      AND ws.normalized_lemma LIKE ?
                    ORDER BY
                        CASE WHEN ws.normalized_lemma = ? THEN 0 ELSE 1 END,
                        wsc.priority_rank, wsc.sense_rank, ws.normalized_lemma
                    LIMIT ?
                    """,
                    (collection_id, pattern, normalized, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT ws.*,
                           COALESCE(uks.recognition_score, 0.0) AS recognition_score,
                           COALESCE(uks.recall_score, 0.0) AS recall_score,
                           COALESCE(uks.production_score, 0.0) AS production_score
                    FROM word_senses ws
                    LEFT JOIN user_knowledge_states uks ON uks.sense_id = ws.id
                    WHERE ws.normalized_lemma LIKE ?
                    ORDER BY
                        CASE WHEN ws.normalized_lemma = ? THEN 0 ELSE 1 END,
                        ws.frequency_rank, ws.normalized_lemma, ws.id
                    LIMIT ?
                    """,
                    (pattern, normalized, limit),
                ).fetchall()
        items = []
        for row in rows:
            item = self._sense_dict(row)
            item.update(
                recognition_score=round(float(row["recognition_score"]), 4),
                recall_score=round(float(row["recall_score"]), 4),
                production_score=round(float(row["production_score"]), 4),
            )
            items.append(item)
        self._attach_pronunciations(items)
        return {"query": query, "count": len(items), "items": items}

    def _attach_pronunciations(self, items: list[dict]) -> None:
        pronunciation_service = PronunciationService(self.database)
        cache: dict[tuple[str, str | None], list[dict]] = {}
        for item in items:
            key = (item["lemma"], item.get("part_of_speech"))
            if key not in cache:
                cache[key] = pronunciation_service.lookup(
                    key[0], part_of_speech=key[1]
                )
            item["pronunciations"] = cache[key]

    @staticmethod
    def _resolve_collection_id(
        connection: sqlite3.Connection,
        collection_id: str | None,
    ) -> tuple[str | None, str]:
        if collection_id:
            if not connection.execute(
                "SELECT 1 FROM vocabulary_collections WHERE id = ?",
                (collection_id,),
            ).fetchone():
                raise ValueError(f"unknown collection_id: {collection_id}")
            return collection_id, "explicit"
        general = connection.execute(
            """
            SELECT id FROM vocabulary_collections
            WHERE kind = 'general'
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()
        if general:
            return str(general["id"]), "auto_general"
        return None, "unfiltered"

    @staticmethod
    def _schedule(step: int, rating: str) -> tuple[int, int, str]:
        if rating == "again":
            return 10 * 60, 0, "learning"
        if rating == "hard":
            return max(1, step) * 24 * 60 * 60, step, "learning"
        schedules = {
            "good": (1, 3, 7, 14, 30, 60),
            "easy": (3, 7, 14, 30, 60, 120),
        }
        days = schedules[rating][min(step, len(schedules[rating]) - 1)]
        next_step = step + 1
        state = "review" if next_step >= 2 else "learning"
        return days * 24 * 60 * 60, next_step, state

    @staticmethod
    def _add_evidence(
        connection: sqlite3.Connection,
        *,
        sense_id: str,
        dimension: str,
        value: float,
        evidence_type: str,
        source_event_id: str,
        now: datetime,
    ) -> None:
        evidence_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO knowledge_evidence(
                id, sense_id, dimension, value, evidence_type, source_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (evidence_id, sense_id, dimension, value, evidence_type, source_event_id, iso(now)),
        )
        score_column = f"{dimension}_score"
        count_column = f"{dimension}_evidence_count"
        if dimension not in {"recognition", "recall", "production"}:
            raise ValueError(f"unsupported dimension: {dimension}")
        connection.execute(
            """
            INSERT INTO user_knowledge_states(sense_id, updated_at)
            VALUES (?, ?)
            ON CONFLICT(sense_id) DO NOTHING
            """,
            (sense_id, iso(now)),
        )
        connection.execute(
            f"""
            UPDATE user_knowledge_states
            SET {score_column} =
                    (({score_column} * {count_column}) + ?) / ({count_column} + 1),
                {count_column} = {count_column} + 1,
                updated_at = ?
            WHERE sense_id = ?
            """,
            (value, iso(now), sense_id),
        )

    @staticmethod
    def _maybe_unlock_recall_card(
        connection: sqlite3.Connection, sense_id: str, now: datetime
    ) -> None:
        state = connection.execute(
            "SELECT * FROM user_knowledge_states WHERE sense_id = ?", (sense_id,)
        ).fetchone()
        if (
            not state
            or state["recognition_evidence_count"] < 2
            or state["recognition_score"] < 0.7
        ):
            return
        now_text = iso(now)
        connection.execute(
            """
            INSERT INTO review_cards(
                id, sense_id, card_type, state, step, due_at, created_at, updated_at
            ) VALUES (?, ?, 'recall', 'new', 0, ?, ?, ?)
            ON CONFLICT(sense_id, card_type) DO NOTHING
            """,
            (str(uuid.uuid4()), sense_id, now_text, now_text, now_text),
        )

    @staticmethod
    def _sense_dict(row: sqlite3.Row) -> dict:
        result = {
            "sense_id": row["id"],
            "lemma": row["lemma"],
            "part_of_speech": row["part_of_speech"],
            "definition_en": row["definition_en"],
            "definition_zh": row["definition_zh"],
            "frequency_rank": row["frequency_rank"],
            "source": row["source"],
            "source_sense_id": row["source_sense_id"],
        }
        if "collection_id" in row.keys():
            result["collection_id"] = row["collection_id"]
            result["collection_rank"] = row["collection_rank"]
            result["collection_sense_rank"] = row["collection_sense_rank"]
        return result

    @staticmethod
    def _card_dict(row: sqlite3.Row) -> dict:
        return {
            "card_id": row["id"],
            "sense_id": row["sense_id"],
            "card_type": row["card_type"],
            "state": row["state"],
            "step": row["step"],
            "due_at": row["due_at"],
            "lemma": row["lemma"],
            "part_of_speech": row["part_of_speech"],
            "definition_en": row["definition_en"],
            "definition_zh": row["definition_zh"],
            "frequency_rank": row["frequency_rank"],
            "source": row["source"],
            "source_sense_id": row["source_sense_id"],
        }

    @staticmethod
    def _review_result(row: sqlite3.Row, *, duplicate: bool) -> dict:
        return {
            "event_id": row["id"],
            "card_id": row["card_id"],
            "rating": row["rating"],
            "interval_seconds": row["interval_seconds"],
            "next_due_at": row["next_due_at"],
            "duplicate": duplicate,
        }
