"""Read-only progress aggregation for cron and interactive reports."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .database import LearningDatabase
from .service import LearningService, iso, utc_now


class ReportService:
    def __init__(self, database: LearningDatabase):
        self.database = database
        self.database.initialize()

    def weekly_report(
        self, *, days: int = 7, now: datetime | None = None
    ) -> dict[str, Any]:
        if not 1 <= days <= 90:
            raise ValueError("days must be between 1 and 90")
        now = now or utc_now()
        start = now - timedelta(days=days)
        start_text = iso(start)
        end_text = iso(now)
        with self.database.connect() as connection:
            review_ratings = self._group_counts(
                connection,
                """
                SELECT rating AS label, COUNT(*) AS count
                FROM review_events
                WHERE reviewed_at >= ? AND reviewed_at <= ?
                GROUP BY rating
                """,
                (start_text, end_text),
                labels=("again", "hard", "good", "easy"),
            )
            review_summary = connection.execute(
                """
                SELECT COUNT(*) AS attempts, COUNT(DISTINCT card_id) AS unique_cards
                FROM review_events
                WHERE reviewed_at >= ? AND reviewed_at <= ?
                """,
                (start_text, end_text),
            ).fetchone()
            production_outcomes = self._group_counts(
                connection,
                """
                SELECT outcome AS label, COUNT(*) AS count
                FROM production_attempts
                WHERE created_at >= ? AND created_at <= ?
                GROUP BY outcome
                """,
                (start_text, end_text),
                labels=("incorrect", "partial", "correct"),
            )
            production_summary = connection.execute(
                """
                SELECT COUNT(*) AS attempts,
                       COUNT(DISTINCT exercise_id) AS unique_exercises,
                       SUM(CASE WHEN revision_of_attempt_id IS NOT NULL THEN 1 ELSE 0 END)
                           AS revisions
                FROM production_attempts
                WHERE created_at >= ? AND created_at <= ?
                """,
                (start_text, end_text),
            ).fetchone()
            assessments = self._group_counts(
                connection,
                """
                SELECT response AS label, COUNT(*) AS count
                FROM assessment_events
                WHERE created_at >= ? AND created_at <= ?
                GROUP BY response
                """,
                (start_text, end_text),
                labels=("known", "unsure", "unknown"),
            )
            reading_summary = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM documents
                     WHERE created_at >= ? AND created_at <= ?) AS documents,
                    COUNT(*) AS encounters,
                    COUNT(DISTINCT sense_id) AS encountered_senses
                FROM encounters
                WHERE created_at >= ? AND created_at <= ?
                """,
                (start_text, end_text, start_text, end_text),
            ).fetchone()
            tutor_lessons = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM reading_tutor_sessions
                    WHERE created_at >= ? AND created_at <= ?
                    """,
                    (start_text, end_text),
                ).fetchone()[0]
            )
            writing_summary = connection.execute(
                """
                SELECT COUNT(*) AS submissions,
                       SUM(CASE WHEN stage = 'revision' THEN 1 ELSE 0 END) AS revisions,
                       COUNT(DISTINCT lesson_id) AS lessons
                FROM writing_submissions
                WHERE created_at >= ? AND created_at <= ?
                """,
                (start_text, end_text),
            ).fetchone()
            cards_created = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM review_cards
                    WHERE created_at >= ? AND created_at <= ?
                    """,
                    (start_text, end_text),
                ).fetchone()[0]
            )
        current = LearningService(self.database).stats(now=now)
        return {
            "period": {
                "days": days,
                "start": start_text,
                "end": end_text,
            },
            "assessment": {
                "responses": assessments,
                "total": sum(assessments.values()),
            },
            "reviews": {
                "attempts": int(review_summary["attempts"]),
                "unique_cards": int(review_summary["unique_cards"]),
                "ratings": review_ratings,
            },
            "reading": {
                "documents": int(reading_summary["documents"] or 0),
                "tutor_lessons": tutor_lessons,
                "encounters": int(reading_summary["encounters"] or 0),
                "encountered_senses": int(reading_summary["encountered_senses"] or 0),
            },
            "production": {
                "attempts": int(production_summary["attempts"] or 0),
                "unique_exercises": int(production_summary["unique_exercises"] or 0),
                "revisions": int(production_summary["revisions"] or 0),
                "outcomes": production_outcomes,
            },
            "writing": {
                "submissions": int(writing_summary["submissions"] or 0),
                "revisions": int(writing_summary["revisions"] or 0),
                "lessons": int(writing_summary["lessons"] or 0),
            },
            "cards_created": cards_created,
            "current_state": current,
        }

    @staticmethod
    def _group_counts(
        connection,
        query: str,
        parameters: tuple[str, str],
        *,
        labels: tuple[str, ...],
    ) -> dict[str, int]:
        counts = {label: 0 for label in labels}
        for row in connection.execute(query, parameters).fetchall():
            counts[row["label"]] = int(row["count"])
        return counts
