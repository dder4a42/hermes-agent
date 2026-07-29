"""Behavior tests for the personal English learning optional skill."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "productivity"
    / "personal-english-learning"
)
SCRIPTS_DIR = SKILL_DIR / "scripts"
CLI_PATH = SCRIPTS_DIR / "english_learning.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from learning_core import LearningDatabase, LearningService, VocabularyEntry


NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def service(tmp_path: Path) -> LearningService:
    return LearningService(LearningDatabase(tmp_path / "learning.db"))


def entry(rank: int, *, source_sense_id: str | None = None) -> VocabularyEntry:
    return VocabularyEntry(
        lemma=f"word-{rank}",
        part_of_speech="noun",
        definition_en=f"definition {rank}",
        definition_zh=f"释义 {rank}",
        frequency_rank=rank,
        source="test-list",
        source_sense_id=source_sense_id or f"sense-{rank}",
    )


def seed(service: LearningService, ranks: list[int]) -> list[str]:
    return [service.upsert_vocabulary(entry(rank), now=NOW)["sense_id"] for rank in ranks]


def test_import_is_source_idempotent_and_preserves_provenance(
    service: LearningService, tmp_path: Path
) -> None:
    source = tmp_path / "vocabulary.jsonl"
    source.write_text(
        json.dumps(
            {
                "lemma": "address",
                "part_of_speech": "verb",
                "definition_en": "to deal with a problem",
                "definition_zh": "处理，应对",
                "frequency_rank": 812,
                "source": "fixture",
                "source_sense_id": "address.v.deal",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    first = service.import_jsonl(source)
    second = service.import_jsonl(source)

    assert first["created"] == 1
    assert second["created"] == 0
    assert second["updated"] == 1
    sample = service.assessment_sample(per_band=1, seed=4)
    assert sample["items"][0]["source"] == "fixture"
    assert sample["items"][0]["source_sense_id"] == "address.v.deal"


def test_missing_source_sense_id_gets_stable_derived_identity(
    service: LearningService,
) -> None:
    value = VocabularyEntry(
        lemma="derive",
        part_of_speech="verb",
        definition_en="to obtain something from a source",
        definition_zh="源自，得到",
        frequency_rank=1500,
        source="personal",
    )

    first = service.upsert_vocabulary(value, now=NOW)
    second = service.upsert_vocabulary(value, now=NOW)

    assert first["identity_derived"] is True
    assert first["sense_id"] == second["sense_id"]
    assert second["created"] is False


def test_assessment_samples_each_frequency_band_and_excludes_answered_items(
    service: LearningService,
) -> None:
    seed(service, [10, 20, 1010, 1020, 2010, 2020, 3010, 3020])

    first = service.assessment_sample(per_band=1, seed=12)
    repeated = service.assessment_sample(per_band=1, seed=12)

    assert first == repeated
    assert {item["frequency_band"] for item in first["items"]} == {
        "1-1000",
        "1001-2000",
        "2001-3000",
        "3001-5000",
    }

    selected = first["items"][0]
    service.record_assessment(
        selected["sense_id"],
        "unsure",
        selected["frequency_band"],
        event_id="assessment-1",
        now=NOW,
    )
    after = service.assessment_sample(per_band=2, seed=12)
    assert selected["sense_id"] not in {item["sense_id"] for item in after["items"]}


def test_assessment_event_is_idempotent_and_updates_projection_once(
    service: LearningService,
) -> None:
    sense_id = seed(service, [50])[0]

    first = service.record_assessment(
        sense_id, "known", "1-1000", event_id="assessment-fixed", now=NOW
    )
    duplicate = service.record_assessment(
        sense_id, "known", "1-1000", event_id="assessment-fixed", now=NOW
    )

    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    with service.database.connect() as connection:
        state = connection.execute(
            "SELECT * FROM user_knowledge_states WHERE sense_id = ?", (sense_id,)
        ).fetchone()
        evidence_count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_evidence WHERE sense_id = ?", (sense_id,)
        ).fetchone()[0]
    assert state["recognition_score"] == pytest.approx(0.9)
    assert state["recognition_evidence_count"] == 1
    assert evidence_count == 1

    with pytest.raises(ValueError, match="different request"):
        service.record_assessment(
            sense_id,
            "unknown",
            "1-1000",
            event_id="assessment-fixed",
            now=NOW,
        )


def test_daily_plan_reduces_then_stops_new_items_for_review_backlog(
    service: LearningService,
) -> None:
    seed(service, list(range(1, 81)))
    initial = service.daily_plan(new_limit=35, review_limit=0, now=NOW)
    assert len(initial["new_items"]) == 35

    reduced = service.daily_plan(
        new_limit=8,
        review_limit=0,
        backlog_reduce_at=30,
        backlog_stop_at=60,
        now=NOW,
    )
    assert reduced["due_count"] == 35
    assert reduced["effective_new_limit"] == 4
    assert len(reduced["new_items"]) == 4

    service.daily_plan(new_limit=30, review_limit=0, now=NOW)
    service.daily_plan(new_limit=30, review_limit=0, now=NOW)
    stopped = service.daily_plan(new_limit=8, review_limit=0, now=NOW)
    assert stopped["due_count"] >= 60
    assert stopped["effective_new_limit"] == 0
    assert stopped["new_items"] == []


def test_daily_plan_prioritizes_assessed_gap_over_unassessed_frequency(
    service: LearningService,
) -> None:
    early, assessed_gap = seed(service, [10, 900])
    service.record_assessment(
        assessed_gap,
        "unknown",
        "1-1000",
        event_id="known-gap",
        now=NOW,
    )

    plan = service.daily_plan(new_limit=1, review_limit=0, now=NOW)

    assert plan["new_items"][0]["sense_id"] == assessed_gap
    assert plan["new_items"][0]["sense_id"] != early


def test_review_is_idempotent_and_reschedules_from_immutable_event(
    service: LearningService,
) -> None:
    seed(service, [100])
    card = service.daily_plan(new_limit=1, now=NOW)["new_items"][0]

    first = service.record_review(
        card["card_id"],
        "good",
        "review-1",
        response_time_ms=1200,
        answer_text="meaning",
        now=NOW,
    )
    duplicate = service.record_review(
        card["card_id"], "good", "review-1", now=NOW
    )

    assert first["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert first["next_due_at"] == duplicate["next_due_at"]
    with service.database.connect() as connection:
        events = connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0]
        card_row = connection.execute(
            "SELECT * FROM review_cards WHERE id = ?", (card["card_id"],)
        ).fetchone()
        state = connection.execute(
            "SELECT * FROM user_knowledge_states WHERE sense_id = ?", (card["sense_id"],)
        ).fetchone()
    assert events == 1
    assert card_row["step"] == 1
    assert state["recognition_evidence_count"] == 1
    assert state["recognition_score"] == pytest.approx(0.75)

    with pytest.raises(ValueError, match="different review"):
        service.record_review(card["card_id"], "easy", "review-1", now=NOW)


def test_stable_recognition_unlocks_one_recall_card(service: LearningService) -> None:
    seed(service, [120])
    card = service.daily_plan(new_limit=1, now=NOW)["new_items"][0]

    service.record_review(card["card_id"], "good", "recognition-1", now=NOW)
    service.record_review(
        card["card_id"],
        "easy",
        "recognition-2",
        now=NOW + timedelta(days=1),
    )
    service.record_review(
        card["card_id"],
        "easy",
        "recognition-2",
        now=NOW + timedelta(days=1),
    )

    with service.database.connect() as connection:
        recall_cards = connection.execute(
            """
            SELECT * FROM review_cards
            WHERE sense_id = ? AND card_type = 'recall'
            """,
            (card["sense_id"],),
        ).fetchall()
    assert len(recall_cards) == 1
    assert recall_cards[0]["state"] == "new"


def test_cli_real_sqlite_end_to_end(tmp_path: Path) -> None:
    database = tmp_path / "profile" / "learning.db"
    vocabulary = tmp_path / "words.jsonl"
    vocabulary.write_text(
        "\n".join(
            json.dumps(
                {
                    "lemma": f"word-{rank}",
                    "part_of_speech": "noun",
                    "definition_en": f"definition {rank}",
                    "definition_zh": f"释义 {rank}",
                    "frequency_rank": rank,
                    "source": "e2e",
                    "source_sense_id": f"e2e-{rank}",
                },
                ensure_ascii=False,
            )
            for rank in (100, 1100, 2100, 3100)
        )
        + "\n",
        encoding="utf-8",
    )

    def invoke(*arguments: str) -> dict:
        completed = subprocess.run(
            [sys.executable, str(CLI_PATH), "--db", str(database), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    imported = invoke("import-jsonl", str(vocabulary))
    sampled = invoke("assessment-sample", "--per-band", "1", "--seed", "3")
    assessed = invoke(
        "assessment-record",
        "--sense-id",
        sampled["items"][0]["sense_id"],
        "--frequency-band",
        sampled["items"][0]["frequency_band"],
        "--response",
        "unknown",
        "--event-id",
        "e2e-assessment",
    )
    plan = invoke("daily-plan", "--new-limit", "1", "--review-limit", "5")
    reviewed = invoke(
        "review",
        "--card-id",
        plan["new_items"][0]["card_id"],
        "--rating",
        "good",
        "--idempotency-key",
        "e2e-review",
    )
    stats = invoke("stats")

    assert imported["created"] == 4
    assert sampled["count"] == 4
    assert assessed["duplicate"] is False
    assert reviewed["interval_seconds"] > 0
    assert stats["vocabulary_senses"] == 4
    assert stats["assessed_senses"] == 1
    assert database.exists()
