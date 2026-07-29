"""Deterministic short-text analysis for vocabulary-first graded reading."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable

from .database import LearningDatabase
from .service import iso, normalize_lemma, utc_now


TOKEN_PATTERN = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
TARGET_STATUSES = frozenset({"accepted", "rejected"})


def lemma_candidates(surface: str) -> list[str]:
    """Return conservative, ordered lemma candidates without external NLP deps."""

    normalized = normalize_lemma(surface.replace("’", "'"))
    candidates = [normalized]
    if normalized.endswith("'s") and len(normalized) > 2:
        candidates.append(normalized[:-2])
    if normalized.endswith("ies") and len(normalized) > 4:
        candidates.append(normalized[:-3] + "y")
    elif normalized.endswith("es") and len(normalized) > 4:
        candidates.extend((normalized[:-2], normalized[:-1]))
    elif normalized.endswith("s") and not normalized.endswith("ss") and len(normalized) > 3:
        candidates.append(normalized[:-1])
    if normalized.endswith("ied") and len(normalized) > 4:
        candidates.append(normalized[:-3] + "y")
    elif normalized.endswith("ed") and len(normalized) > 4:
        stem = normalized[:-2]
        candidates.extend((stem, stem + "e"))
        if len(stem) > 2 and stem[-1] == stem[-2]:
            candidates.append(stem[:-1])
    if normalized.endswith("ing") and len(normalized) > 5:
        stem = normalized[:-3]
        candidates.extend((stem, stem + "e"))
        if len(stem) > 2 and stem[-1] == stem[-2]:
            candidates.append(stem[:-1])
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


class ReadingService:
    def __init__(self, database: LearningDatabase):
        self.database = database
        self.database.initialize()

    def analyze_text(
        self,
        content: str,
        *,
        title: str = "Untitled reading",
        source: str = "user_text",
        target_limit: int = 5,
        now: datetime | None = None,
    ) -> dict:
        content = content.strip()
        title = title.strip() or "Untitled reading"
        source = source.strip() or "user_text"
        if not content:
            raise ValueError("content cannot be empty")
        if not 1 <= target_limit <= 20:
            raise ValueError("target_limit must be between 1 and 20")
        now = now or utc_now()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        document_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"personal-english:{source}:{content_hash}")
        )

        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM documents WHERE source = ? AND content_hash = ?",
                (source, content_hash),
            ).fetchone()
            if not existing:
                connection.execute(
                    """
                    INSERT INTO documents(id, title, source, content, content_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (document_id, title, source, content, content_hash, iso(now)),
                )
                self._index_document(connection, document_id, content)
                self._rank_targets(connection, document_id)
            else:
                document_id = existing["id"]
        return self.get_analysis(document_id, target_limit=target_limit)

    def get_analysis(self, document_id: str, *, target_limit: int = 5) -> dict:
        if not 1 <= target_limit <= 20:
            raise ValueError("target_limit must be between 1 and 20")
        with self.database.connect() as connection:
            document = connection.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
            if not document:
                raise ValueError(f"unknown document_id: {document_id}")
            tokens = connection.execute(
                "SELECT * FROM document_tokens WHERE document_id = ? ORDER BY token_index",
                (document_id,),
            ).fetchall()
            matched_token_ids = {
                row["token_id"]
                for row in connection.execute(
                    """
                    SELECT DISTINCT dts.token_id
                    FROM document_token_senses dts
                    JOIN document_tokens dt ON dt.id = dts.token_id
                    WHERE dt.document_id = ?
                    """,
                    (document_id,),
                ).fetchall()
            }
            known_token_ids = {
                row["token_id"]
                for row in connection.execute(
                    """
                    SELECT dts.token_id
                    FROM document_token_senses dts
                    JOIN document_tokens dt ON dt.id = dts.token_id
                    JOIN user_knowledge_states uks ON uks.sense_id = dts.sense_id
                    WHERE dt.document_id = ? AND dt.match_status = 'unique'
                      AND uks.recognition_score >= 0.7
                    """,
                    (document_id,),
                ).fetchall()
            }
            target_rows = connection.execute(
                """
                SELECT rt.*, ws.lemma, ws.part_of_speech, ws.definition_en,
                       ws.definition_zh, ws.frequency_rank, ws.source,
                       ws.source_sense_id,
                       COUNT(DISTINCT dt.id) AS occurrence_count,
                       MIN(dt.surface) AS example_surface
                FROM reading_targets rt
                JOIN word_senses ws ON ws.id = rt.sense_id
                LEFT JOIN document_token_senses dts ON dts.sense_id = rt.sense_id
                LEFT JOIN document_tokens dt
                  ON dt.id = dts.token_id AND dt.document_id = rt.document_id
                WHERE rt.document_id = ?
                GROUP BY rt.document_id, rt.sense_id
                ORDER BY
                    CASE rt.status WHEN 'accepted' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
                    rt.priority_score DESC, ws.frequency_rank, ws.normalized_lemma
                """,
                (document_id,),
            ).fetchall()
            ambiguity_rows = connection.execute(
                """
                SELECT dt.id AS token_id, dt.token_index, dt.surface, dt.start_offset,
                       dt.end_offset, ws.*
                FROM document_tokens dt
                JOIN document_token_senses dts ON dts.token_id = dt.id
                JOIN word_senses ws ON ws.id = dts.sense_id
                WHERE dt.document_id = ? AND dt.match_status = 'ambiguous'
                ORDER BY dt.token_index, ws.frequency_rank, ws.id
                """,
                (document_id,),
            ).fetchall()

        total = len(tokens)
        matched = len(matched_token_ids)
        known = len(known_token_ids)
        return {
            "document_id": document_id,
            "title": document["title"],
            "source": document["source"],
            "token_count": total,
            "catalog_matched_tokens": matched,
            "unambiguous_known_tokens": known,
            "catalog_coverage": round(matched / total, 4) if total else 0.0,
            "known_coverage": round(known / total, 4) if total else 0.0,
            "targets": [self._target_dict(row) for row in target_rows[:target_limit]],
            "ambiguous_matches": self._group_ambiguities(ambiguity_rows),
            "unmatched_tokens": self._unmatched_summary(tokens),
        }

    def confirm_targets(
        self,
        document_id: str,
        decisions: Iterable[dict[str, str]],
        *,
        now: datetime | None = None,
    ) -> dict:
        now = now or utc_now()
        normalized_decisions = list(decisions)
        if not normalized_decisions:
            raise ValueError("at least one target decision is required")
        accepted = 0
        rejected = 0
        cards_created = 0
        encounters_created = 0
        with self.database.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM documents WHERE id = ?", (document_id,)
            ).fetchone():
                raise ValueError(f"unknown document_id: {document_id}")
            for decision in normalized_decisions:
                sense_id = str(decision.get("sense_id", "")).strip()
                status = str(decision.get("status", "")).strip()
                if not sense_id or status not in TARGET_STATUSES:
                    raise ValueError(
                        "each decision requires sense_id and status accepted/rejected"
                    )
                token_rows = connection.execute(
                    """
                    SELECT dt.id FROM document_tokens dt
                    JOIN document_token_senses dts ON dts.token_id = dt.id
                    WHERE dt.document_id = ? AND dts.sense_id = ?
                    """,
                    (document_id, sense_id),
                ).fetchall()
                if not token_rows:
                    raise ValueError(
                        f"sense_id {sense_id} does not occur in document {document_id}"
                    )
                existing = connection.execute(
                    """
                    SELECT status FROM reading_targets
                    WHERE document_id = ? AND sense_id = ?
                    """,
                    (document_id, sense_id),
                ).fetchone()
                if existing:
                    connection.execute(
                        """
                        UPDATE reading_targets SET status = ?, decided_at = ?
                        WHERE document_id = ? AND sense_id = ?
                        """,
                        (status, iso(now), document_id, sense_id),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO reading_targets(
                            document_id, sense_id, status, priority_score,
                            selection_reason, decided_at
                        ) VALUES (?, ?, ?, 0.0, 'manual_disambiguation', ?)
                        """,
                        (document_id, sense_id, status, iso(now)),
                    )
                if status == "rejected":
                    rejected += 1
                    continue
                accepted += 1
                for token_row in token_rows:
                    encounter = connection.execute(
                        """
                        INSERT INTO encounters(
                            id, sense_id, document_id, token_id, encounter_type, created_at
                        ) VALUES (?, ?, ?, ?, 'reading', ?)
                        ON CONFLICT(sense_id, document_id, token_id, encounter_type)
                        DO NOTHING
                        """,
                        (
                            str(uuid.uuid4()),
                            sense_id,
                            document_id,
                            token_row["id"],
                            iso(now),
                        ),
                    )
                    encounters_created += int(encounter.rowcount > 0)
                state = connection.execute(
                    """
                    SELECT recognition_score FROM user_knowledge_states
                    WHERE sense_id = ?
                    """,
                    (sense_id,),
                ).fetchone()
                if not state or state["recognition_score"] < 0.7:
                    card = connection.execute(
                        """
                        INSERT INTO review_cards(
                            id, sense_id, card_type, state, step, due_at,
                            created_at, updated_at
                        ) VALUES (?, ?, 'recognition', 'new', 0, ?, ?, ?)
                        ON CONFLICT(sense_id, card_type) DO NOTHING
                        """,
                        (str(uuid.uuid4()), sense_id, iso(now), iso(now), iso(now)),
                    )
                    cards_created += int(card.rowcount > 0)
        return {
            "document_id": document_id,
            "accepted": accepted,
            "rejected": rejected,
            "encounters_created": encounters_created,
            "cards_created": cards_created,
        }

    def _index_document(
        self, connection: sqlite3.Connection, document_id: str, content: str
    ) -> None:
        for token_index, match in enumerate(TOKEN_PATTERN.finditer(content)):
            surface = match.group(0)
            senses = self._lookup_senses(connection, lemma_candidates(surface))
            status = "unmatched"
            if len(senses) == 1:
                status = "unique"
            elif len(senses) > 1:
                status = "ambiguous"
            token_id = str(uuid.uuid5(uuid.UUID(document_id), str(token_index)))
            connection.execute(
                """
                INSERT INTO document_tokens(
                    id, document_id, token_index, surface, normalized,
                    start_offset, end_offset, match_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    document_id,
                    token_index,
                    surface,
                    normalize_lemma(surface),
                    match.start(),
                    match.end(),
                    status,
                ),
            )
            for sense, matched_lemma in senses:
                connection.execute(
                    """
                    INSERT INTO document_token_senses(token_id, sense_id, matched_lemma)
                    VALUES (?, ?, ?)
                    """,
                    (token_id, sense["id"], matched_lemma),
                )

    @staticmethod
    def _lookup_senses(
        connection: sqlite3.Connection, candidates: list[str]
    ) -> list[tuple[sqlite3.Row, str]]:
        results: list[tuple[sqlite3.Row, str]] = []
        seen: set[str] = set()
        for candidate in candidates:
            rows = connection.execute(
                """
                SELECT * FROM word_senses
                WHERE normalized_lemma = ?
                ORDER BY frequency_rank, source, source_sense_id
                """,
                (candidate,),
            ).fetchall()
            for row in rows:
                if row["id"] not in seen:
                    results.append((row, candidate))
                    seen.add(row["id"])
        return results

    @staticmethod
    def _rank_targets(connection: sqlite3.Connection, document_id: str) -> None:
        rows = connection.execute(
            """
            SELECT dts.sense_id, COUNT(*) AS occurrence_count,
                   ws.frequency_rank,
                   COALESCE(uks.recognition_score, 0.0) AS recognition_score,
                   COALESCE(uks.recognition_evidence_count, 0) AS evidence_count
            FROM document_token_senses dts
            JOIN document_tokens dt ON dt.id = dts.token_id
            JOIN word_senses ws ON ws.id = dts.sense_id
            LEFT JOIN user_knowledge_states uks ON uks.sense_id = dts.sense_id
            WHERE dt.document_id = ? AND dt.match_status = 'unique'
              AND COALESCE(uks.recognition_score, 0.0) < 0.7
            GROUP BY dts.sense_id
            """,
            (document_id,),
        ).fetchall()
        for row in rows:
            gap_bonus = 100.0 if row["evidence_count"] > 0 else 0.0
            rank = row["frequency_rank"] or 100_000
            frequency_bonus = 50.0 / (1.0 + rank / 1000.0)
            repeat_bonus = min(max(row["occurrence_count"] - 1, 0), 5) * 5.0
            score = gap_bonus + frequency_bonus + repeat_bonus
            reasons = ["assessed_gap" if gap_bonus else "unassessed"]
            if row["occurrence_count"] > 1:
                reasons.append("repeated_in_text")
            reasons.append("frequency_ranked")
            connection.execute(
                """
                INSERT INTO reading_targets(
                    document_id, sense_id, status, priority_score, selection_reason
                ) VALUES (?, ?, 'candidate', ?, ?)
                """,
                (document_id, row["sense_id"], score, ",".join(reasons)),
            )

    @staticmethod
    def _target_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sense_id": row["sense_id"],
            "lemma": row["lemma"],
            "part_of_speech": row["part_of_speech"],
            "definition_en": row["definition_en"],
            "definition_zh": row["definition_zh"],
            "frequency_rank": row["frequency_rank"],
            "source": row["source"],
            "source_sense_id": row["source_sense_id"],
            "status": row["status"],
            "priority_score": round(float(row["priority_score"]), 4),
            "selection_reason": row["selection_reason"].split(","),
            "occurrence_count": int(row["occurrence_count"]),
            "example_surface": row["example_surface"],
        }

    @staticmethod
    def _group_ambiguities(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = grouped.setdefault(
                row["token_id"],
                {
                    "token_index": row["token_index"],
                    "surface": row["surface"],
                    "start_offset": row["start_offset"],
                    "end_offset": row["end_offset"],
                    "requires_disambiguation": True,
                    "senses": [],
                },
            )
            item["senses"].append(
                {
                    "sense_id": row["id"],
                    "lemma": row["lemma"],
                    "part_of_speech": row["part_of_speech"],
                    "definition_en": row["definition_en"],
                    "definition_zh": row["definition_zh"],
                    "source": row["source"],
                    "source_sense_id": row["source_sense_id"],
                }
            )
        return list(grouped.values())

    @staticmethod
    def _unmatched_summary(tokens: list[sqlite3.Row]) -> list[dict[str, Any]]:
        counts: dict[str, int] = defaultdict(int)
        surfaces: dict[str, str] = {}
        for token in tokens:
            if token["match_status"] != "unmatched":
                continue
            counts[token["normalized"]] += 1
            surfaces.setdefault(token["normalized"], token["surface"])
        return [
            {"normalized": normalized, "surface": surfaces[normalized], "count": count}
            for normalized, count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0])
            )
        ]
