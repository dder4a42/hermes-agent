"""Lightweight cloze and sentence-production practice."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import Any

from .database import LearningDatabase
from .service import LearningService, iso, utc_now


OUTCOME_VALUES = {"incorrect": 0.0, "partial": 0.45, "correct": 0.9}
OUTCOMES = frozenset(OUTCOME_VALUES)


class ProductionService:
    def __init__(self, database: LearningDatabase):
        self.database = database
        self.database.initialize()

    def plan_for_document(
        self,
        document_id: str,
        *,
        limit: int = 6,
        now: datetime | None = None,
    ) -> dict:
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        now = now or utc_now()
        with self.database.connect() as connection:
            document = connection.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if not document:
                raise ValueError(f"unknown document_id: {document_id}")
            accepted = connection.execute(
                """
                SELECT rt.sense_id, ws.lemma, ws.definition_en, ws.definition_zh
                FROM reading_targets rt
                JOIN word_senses ws ON ws.id = rt.sense_id
                WHERE rt.document_id = ? AND rt.status = 'accepted'
                ORDER BY rt.priority_score DESC, ws.frequency_rank, ws.normalized_lemma
                """,
                (document_id,),
            ).fetchall()
            for target in accepted:
                token = connection.execute(
                    """
                    SELECT dt.* FROM document_tokens dt
                    JOIN document_token_senses dts ON dts.token_id = dt.id
                    WHERE dt.document_id = ? AND dts.sense_id = ?
                    ORDER BY dt.token_index LIMIT 1
                    """,
                    (document_id, target["sense_id"]),
                ).fetchone()
                if not token:
                    continue
                sentence, relative_start, relative_end = self._sentence_for_token(
                    document["content"], token["start_offset"], token["end_offset"]
                )
                cloze_text = (
                    sentence[:relative_start] + "____" + sentence[relative_end:]
                )
                self._insert_exercise(
                    connection,
                    exercise_id=self._exercise_id(
                        document_id, target["sense_id"], "cloze", token["id"]
                    ),
                    sense_id=target["sense_id"],
                    document_id=document_id,
                    source_token_id=token["id"],
                    exercise_type="cloze",
                    prompt=f"Complete the sentence with the missing English word: {cloze_text}",
                    expected_answer=token["surface"],
                    now=now,
                )
                meaning = target["definition_zh"] or target["definition_en"]
                self._insert_exercise(
                    connection,
                    exercise_id=self._exercise_id(
                        document_id, target["sense_id"], "sentence", None
                    ),
                    sense_id=target["sense_id"],
                    document_id=document_id,
                    source_token_id=None,
                    exercise_type="sentence",
                    prompt=(
                        f"Write one short English sentence using '{target['lemma']}' "
                        f"with this meaning: {meaning}"
                    ),
                    expected_answer=None,
                    now=now,
                )
            exercises = connection.execute(
                """
                SELECT pe.*, ws.lemma, ws.definition_en, ws.definition_zh,
                       pa.id AS latest_attempt_id, pa.answer_text AS latest_answer,
                       pa.outcome AS latest_outcome, pa.feedback AS latest_feedback
                FROM production_exercises pe
                JOIN word_senses ws ON ws.id = pe.sense_id
                JOIN reading_targets rt
                  ON rt.document_id = pe.document_id AND rt.sense_id = pe.sense_id
                LEFT JOIN production_attempts pa ON pa.id = (
                    SELECT inner_pa.id FROM production_attempts inner_pa
                    WHERE inner_pa.exercise_id = pe.id
                    ORDER BY inner_pa.created_at DESC, inner_pa.id DESC LIMIT 1
                )
                WHERE pe.document_id = ?
                ORDER BY
                    rt.priority_score DESC,
                    CASE pe.exercise_type WHEN 'cloze' THEN 0 ELSE 1 END,
                    pe.id
                LIMIT ?
                """,
                (document_id, limit),
            ).fetchall()
        return {
            "document_id": document_id,
            "count": len(exercises),
            "exercises": [self._exercise_dict(row) for row in exercises],
        }

    def submit_attempt(
        self,
        exercise_id: str,
        answer_text: str,
        outcome: str | None,
        idempotency_key: str,
        *,
        feedback: str | None = None,
        revision_of_attempt_id: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        answer_text = answer_text.strip()
        idempotency_key = idempotency_key.strip()
        if not answer_text:
            raise ValueError("answer_text is required")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        now = now or utc_now()
        with self.database.connect() as connection:
            exercise = connection.execute(
                "SELECT * FROM production_exercises WHERE id = ?", (exercise_id,)
            ).fetchone()
            if not exercise:
                raise ValueError(f"unknown exercise_id: {exercise_id}")
            auto_graded = outcome is None
            if auto_graded:
                if exercise["exercise_type"] != "cloze":
                    raise ValueError(
                        "outcome is required for open sentence-production exercises"
                    )
                outcome = (
                    "correct"
                    if answer_text.casefold()
                    == str(exercise["expected_answer"] or "").strip().casefold()
                    else "incorrect"
                )
            if outcome not in OUTCOMES:
                raise ValueError("outcome must be correct, partial, or incorrect")
            if (
                not auto_graded
                and outcome != "correct"
                and not (feedback and feedback.strip())
            ):
                raise ValueError(
                    "feedback is required for partial or incorrect open responses"
                )
            duplicate = connection.execute(
                "SELECT * FROM production_attempts WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if duplicate:
                if (
                    duplicate["exercise_id"] != exercise_id
                    or duplicate["answer_text"] != answer_text
                    or duplicate["outcome"] != outcome
                    or duplicate["revision_of_attempt_id"] != revision_of_attempt_id
                ):
                    raise ValueError(
                        "idempotency_key was already used for a different attempt"
                    )
                return self._attempt_dict(duplicate, duplicate=True)
            if revision_of_attempt_id:
                original = connection.execute(
                    "SELECT * FROM production_attempts WHERE id = ?",
                    (revision_of_attempt_id,),
                ).fetchone()
                if not original:
                    raise ValueError(
                        f"unknown revision_of_attempt_id: {revision_of_attempt_id}"
                    )
                if original["exercise_id"] != exercise_id:
                    raise ValueError("revision must belong to the same exercise")
            attempt_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO production_attempts(
                    id, exercise_id, idempotency_key, answer_text, outcome,
                    feedback, revision_of_attempt_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    exercise_id,
                    idempotency_key,
                    answer_text,
                    outcome,
                    feedback,
                    revision_of_attempt_id,
                    iso(now),
                ),
            )
            LearningService._add_evidence(
                connection,
                sense_id=exercise["sense_id"],
                dimension="production",
                value=OUTCOME_VALUES[outcome],
                evidence_type="production_attempt",
                source_event_id=attempt_id,
                now=now,
            )
            card_created = 0
            if outcome != "correct":
                card = connection.execute(
                    """
                    INSERT INTO review_cards(
                        id, sense_id, card_type, state, step, due_at,
                        created_at, updated_at
                    ) VALUES (?, ?, 'recall', 'new', 0, ?, ?, ?)
                    ON CONFLICT(sense_id, card_type) DO NOTHING
                    """,
                    (
                        str(uuid.uuid4()),
                        exercise["sense_id"],
                        iso(now),
                        iso(now),
                        iso(now),
                    ),
                )
                card_created = int(card.rowcount > 0)
            attempt = connection.execute(
                "SELECT * FROM production_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        result = self._attempt_dict(attempt, duplicate=False)
        result["recall_card_created"] = card_created
        return result

    @staticmethod
    def _exercise_id(
        document_id: str, sense_id: str, exercise_type: str, token_id: str | None
    ) -> str:
        material = f"{document_id}:{sense_id}:{exercise_type}:{token_id or '-'}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, material))

    @staticmethod
    def _insert_exercise(
        connection: sqlite3.Connection,
        *,
        exercise_id: str,
        sense_id: str,
        document_id: str,
        source_token_id: str | None,
        exercise_type: str,
        prompt: str,
        expected_answer: str | None,
        now: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO production_exercises(
                id, sense_id, document_id, source_token_id, exercise_type,
                prompt, expected_answer, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                exercise_id,
                sense_id,
                document_id,
                source_token_id,
                exercise_type,
                prompt,
                expected_answer,
                iso(now),
            ),
        )

    @staticmethod
    def _sentence_for_token(
        content: str, start_offset: int, end_offset: int
    ) -> tuple[str, int, int]:
        boundaries = ".!?\n"
        sentence_start = max(content.rfind(mark, 0, start_offset) for mark in boundaries)
        sentence_start = 0 if sentence_start < 0 else sentence_start + 1
        following = [
            position
            for mark in boundaries
            if (position := content.find(mark, end_offset)) >= 0
        ]
        sentence_end = min(following) + 1 if following else len(content)
        while sentence_start < sentence_end and content[sentence_start].isspace():
            sentence_start += 1
        sentence = content[sentence_start:sentence_end].strip()
        relative_start = start_offset - sentence_start
        relative_end = relative_start + (end_offset - start_offset)
        return sentence, relative_start, relative_end

    @staticmethod
    def _exercise_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "exercise_id": row["id"],
            "sense_id": row["sense_id"],
            "exercise_type": row["exercise_type"],
            "prompt": row["prompt"],
            "expected_answer": row["expected_answer"],
            "lemma": row["lemma"],
            "definition_en": row["definition_en"],
            "definition_zh": row["definition_zh"],
            "latest_attempt": (
                {
                    "attempt_id": row["latest_attempt_id"],
                    "answer_text": row["latest_answer"],
                    "outcome": row["latest_outcome"],
                    "feedback": row["latest_feedback"],
                }
                if row["latest_attempt_id"]
                else None
            ),
        }

    @staticmethod
    def _attempt_dict(row: sqlite3.Row, *, duplicate: bool) -> dict[str, Any]:
        return {
            "attempt_id": row["id"],
            "exercise_id": row["exercise_id"],
            "answer_text": row["answer_text"],
            "outcome": row["outcome"],
            "feedback": row["feedback"],
            "revision_of_attempt_id": row["revision_of_attempt_id"],
            "duplicate": duplicate,
        }
