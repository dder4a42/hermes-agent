"""Behavior tests for the personal English learning optional skill."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import httpx


SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "productivity"
    / "personal-english-learning"
)
SCRIPTS_DIR = SKILL_DIR / "scripts"
CLI_PATH = SCRIPTS_DIR / "english_learning.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from learning_core import (
    CollectionSpec,
    ExamService,
    LearningDatabase,
    LexicalAnalysisService,
    LexicalInferenceService,
    LearningService,
    PronunciationService,
    ProductionService,
    ReadingService,
    ReadingTutorService,
    ReportService,
    WritingCoachService,
    VocabularyEntry,
    VocabularyBuilder,
    frequency_rank_map,
    load_ranked_lemmas,
)
from learning_core.reading import lemma_candidates
from learning_core.reading_tutor import _parse_json_object
from learning_core.pronunciation import arpabet_to_ipa, arpabet_to_respelling
from learning_core.vocabulary_builder import normalize_part_of_speech
from learning_web import SESSION_HEADER, create_app
from tools.blueprints import blueprint_to_job_spec, parse_blueprint


NOW = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)


def test_model_json_parser_accepts_explanatory_wrapper() -> None:
    assert _parse_json_object('Result follows:\n{"analyses": []}\nDone.') == {
        "analyses": []
    }


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


def _wordnet_fixture(path: Path) -> Path:
    entries = {
        "a": {
            "n": {
                "sense": [{"id": "a%1:23:01::", "synset": "a-n-1"}]
            }
        },
        "address": {
            "n-1": {
                "sense": [
                    {"id": "address%1:10:00::", "synset": "address-n-1"},
                    {"id": "address%1:10:01::", "synset": "address-n-2"},
                ]
            },
            "v": {
                "sense": [
                    {"id": "address%2:32:00::", "synset": "address-v-1"}
                ]
            },
        },
        "derive": {
            "v": {
                "sense": [
                    {
                        "id": "derive%2:40:00::",
                        "synset": "derive-v-1",
                        "derivation": [
                            "derivation%1:22:00::",
                            "derivative%5:00:00:derived:00",
                        ],
                    }
                ]
            }
        },
    }
    noun_synsets = {
        "a-n-1": {"definition": ["a metric unit used for wavelengths"]},
        "address-n-1": {"definition": ["the place where something is located"]},
        "address-n-2": {"definition": ["a formal spoken communication"]},
    }
    verb_synsets = {
        "address-v-1": {"definition": ["to deal with a problem"]},
        "derive-v-1": {"definition": ["to obtain something from a source"]},
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("entries-a.json", json.dumps(entries))
        archive.writestr("noun.communication.json", json.dumps(noun_synsets))
        archive.writestr("verb.communication.json", json.dumps(verb_synsets))
        archive.writestr("frames.json", "{}")
        archive.writestr(
            "index.sense",
            "\n".join(
                (
                    "a%1:23:01:: 00000000 1 20",
                    "address%1:10:00:: 00000001 1 4",
                    "address%1:10:01:: 00000002 2 0",
                    "address%2:32:00:: 00000003 1 8",
                    "derive%2:40:00:: 00000004 1 2",
                )
            )
            + "\n",
        )
    return path


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
        now=NOW + timedelta(days=1),
    )
    assert reduced["due_count"] == 35
    assert reduced["effective_new_limit"] == 4
    assert len(reduced["new_items"]) == 4

    service.daily_plan(new_limit=30, review_limit=0, now=NOW + timedelta(days=2))
    service.daily_plan(new_limit=30, review_limit=0, now=NOW + timedelta(days=3))
    stopped = service.daily_plan(
        new_limit=8, review_limit=0, now=NOW + timedelta(days=4)
    )
    assert stopped["due_count"] >= 60
    assert stopped["effective_new_limit"] == 0
    assert stopped["new_items"] == []


def test_daily_plan_is_idempotent_for_date_and_collection(
    service: LearningService,
) -> None:
    seed(service, list(range(1, 21)))

    first = service.daily_plan(new_limit=8, review_limit=30, now=NOW)
    repeated = service.daily_plan(new_limit=20, review_limit=100, now=NOW)

    assert first["reused_plan"] is False
    assert repeated["reused_plan"] is True
    assert repeated["requested_new_limit"] == 8
    assert [item["card_id"] for item in repeated["new_items"]] == [
        item["card_id"] for item in first["new_items"]
    ]
    with service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_plans").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM review_cards").fetchone()[0] == 8


def test_concurrent_daily_plan_requests_create_one_plan(
    service: LearningService,
) -> None:
    seed(service, list(range(1, 21)))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: service.daily_plan(
                    new_limit=8, review_limit=30, now=NOW
                ),
                range(2),
            )
        )

    assert {result["reused_plan"] for result in results} == {False, True}
    assert {
        tuple(item["card_id"] for item in result["new_items"])
        for result in results
    } == {tuple(item["card_id"] for item in results[0]["new_items"])}
    with service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_plans").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM review_cards").fetchone()[0] == 8


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


def test_conservative_lemma_candidates_cover_common_inflections() -> None:
    assert lemma_candidates("agents")[:2] == ["agents", "agent"]
    assert "study" in lemma_candidates("studied")
    assert "run" in lemma_candidates("running")
    assert lemma_candidates("address") == ["address"]


def test_reading_analysis_reports_coverage_targets_and_ambiguity(
    service: LearningService,
) -> None:
    research = service.upsert_vocabulary(
        VocabularyEntry(
            "research", "noun", "systematic study", "研究", 400, "fixture", "research.n"
        ),
        now=NOW,
    )["sense_id"]
    agent = service.upsert_vocabulary(
        VocabularyEntry(
            "agent", "noun", "a person or system that acts", "智能体", 900, "fixture", "agent.n"
        ),
        now=NOW,
    )["sense_id"]
    address_problem = service.upsert_vocabulary(
        VocabularyEntry(
            "address", "verb", "to deal with a problem", "处理", 800, "fixture", "address.v"
        ),
        now=NOW,
    )["sense_id"]
    service.upsert_vocabulary(
        VocabularyEntry(
            "address", "noun", "location details", "地址", 800, "fixture", "address.n"
        ),
        now=NOW,
    )
    service.record_assessment(
        research, "known", "1-1000", event_id="research-known", now=NOW
    )
    service.record_assessment(
        agent, "unknown", "1-1000", event_id="agent-gap", now=NOW
    )
    reading = ReadingService(service.database)

    result = reading.analyze_text(
        "Research agents address problems. Agents improve research.",
        title="Agent systems",
        target_limit=3,
        now=NOW,
    )

    assert result["token_count"] == 7
    assert result["catalog_matched_tokens"] == 5
    assert result["unambiguous_known_tokens"] == 2
    assert result["catalog_coverage"] == pytest.approx(5 / 7, abs=0.0001)
    assert result["known_coverage"] == pytest.approx(2 / 7, abs=0.0001)
    assert [target["sense_id"] for target in result["targets"]] == [agent]
    assert result["targets"][0]["occurrence_count"] == 2
    assert "assessed_gap" in result["targets"][0]["selection_reason"]
    assert len(result["ambiguous_matches"]) == 1
    ambiguous_sense_ids = {
        sense["sense_id"] for sense in result["ambiguous_matches"][0]["senses"]
    }
    assert address_problem in ambiguous_sense_ids
    assert len(ambiguous_sense_ids) == 2

    manual = reading.confirm_targets(
        result["document_id"],
        [{"sense_id": address_problem, "status": "accepted"}],
        now=NOW,
    )
    assert manual["encounters_created"] == 1

    repeated = reading.analyze_text(
        "Research agents address problems. Agents improve research.",
        title="A different title does not duplicate content",
        target_limit=3,
        now=NOW,
    )
    assert repeated["document_id"] == result["document_id"]
    with service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM document_tokens").fetchone()[0] == 7


def test_reading_confirmation_creates_idempotent_encounters_and_card(
    service: LearningService,
) -> None:
    sense_id = service.upsert_vocabulary(
        VocabularyEntry(
            "agent", "noun", "a system that acts", "智能体", 900, "fixture", "agent.n"
        ),
        now=NOW,
    )["sense_id"]
    reading = ReadingService(service.database)
    analysis = reading.analyze_text("An agent helps another agent.", now=NOW)

    first = reading.confirm_targets(
        analysis["document_id"],
        [{"sense_id": sense_id, "status": "accepted"}],
        now=NOW,
    )
    repeated = reading.confirm_targets(
        analysis["document_id"],
        [{"sense_id": sense_id, "status": "accepted"}],
        now=NOW,
    )

    assert first["encounters_created"] == 2
    assert first["cards_created"] == 1
    assert repeated["encounters_created"] == 0
    assert repeated["cards_created"] == 0
    with service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM encounters").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM review_cards").fetchone()[0] == 1


def _accepted_reading(
    service: LearningService, text: str = "An agent helps another agent."
) -> tuple[str, str]:
    sense_id = service.upsert_vocabulary(
        VocabularyEntry(
            "agent", "noun", "a system that acts", "智能体", 900, "fixture", "agent.n"
        ),
        now=NOW,
    )["sense_id"]
    reading = ReadingService(service.database)
    analysis = reading.analyze_text(text, now=NOW)
    reading.confirm_targets(
        analysis["document_id"],
        [{"sense_id": sense_id, "status": "accepted"}],
        now=NOW,
    )
    return analysis["document_id"], sense_id


def test_production_plan_is_idempotent_and_uses_original_context(
    service: LearningService,
) -> None:
    document_id, sense_id = _accepted_reading(service)
    production = ProductionService(service.database)

    first = production.plan_for_document(document_id, now=NOW)
    repeated = production.plan_for_document(document_id, now=NOW)

    assert {**first, "cached": None} == {**repeated, "cached": None}
    assert first["count"] == 2
    assert {item["exercise_type"] for item in first["exercises"]} == {
        "cloze",
        "sentence",
    }
    cloze = next(item for item in first["exercises"] if item["exercise_type"] == "cloze")
    sentence = next(
        item for item in first["exercises"] if item["exercise_type"] == "sentence"
    )
    assert "____" in cloze["prompt"]
    assert cloze["expected_answer"] == "agent"
    assert sentence["sense_id"] == sense_id
    assert "智能体" in sentence["prompt"]
    with service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM production_exercises").fetchone()[0] == 2


def test_cloze_attempt_auto_grades_and_revision_updates_production_projection(
    service: LearningService,
) -> None:
    document_id, sense_id = _accepted_reading(service)
    production = ProductionService(service.database)
    cloze = next(
        item
        for item in production.plan_for_document(document_id, now=NOW)["exercises"]
        if item["exercise_type"] == "cloze"
    )

    wrong = production.submit_attempt(
        cloze["exercise_id"], "agency", None, "cloze-1", now=NOW
    )
    revision = production.submit_attempt(
        cloze["exercise_id"],
        "agent",
        None,
        "cloze-2",
        revision_of_attempt_id=wrong["attempt_id"],
        now=NOW + timedelta(minutes=1),
    )
    duplicate = production.submit_attempt(
        cloze["exercise_id"],
        "agent",
        None,
        "cloze-2",
        revision_of_attempt_id=wrong["attempt_id"],
        now=NOW + timedelta(minutes=1),
    )

    assert wrong["outcome"] == "incorrect"
    assert wrong["recall_card_created"] == 1
    assert revision["outcome"] == "correct"
    assert revision["revision_of_attempt_id"] == wrong["attempt_id"]
    assert duplicate["duplicate"] is True
    with service.database.connect() as connection:
        state = connection.execute(
            "SELECT * FROM user_knowledge_states WHERE sense_id = ?", (sense_id,)
        ).fetchone()
        attempts = connection.execute("SELECT COUNT(*) FROM production_attempts").fetchone()[0]
    assert attempts == 2
    assert state["production_evidence_count"] == 2
    assert state["production_score"] == pytest.approx(0.45)


def test_sentence_attempt_requires_structured_outcome_and_revision_same_exercise(
    service: LearningService,
) -> None:
    document_id, _ = _accepted_reading(service)
    production = ProductionService(service.database)
    exercises = production.plan_for_document(document_id, now=NOW)["exercises"]
    sentence = next(item for item in exercises if item["exercise_type"] == "sentence")
    cloze = next(item for item in exercises if item["exercise_type"] == "cloze")

    with pytest.raises(ValueError, match="outcome is required"):
        production.submit_attempt(
            sentence["exercise_id"], "The agent work.", None, "sentence-missing", now=NOW
        )
    with pytest.raises(ValueError, match="feedback is required"):
        production.submit_attempt(
            sentence["exercise_id"],
            "The agent work.",
            "partial",
            "sentence-no-feedback",
            now=NOW,
        )
    partial = production.submit_attempt(
        sentence["exercise_id"],
        "The agent work.",
        "partial",
        "sentence-1",
        feedback="Use third-person singular: works.",
        now=NOW,
    )
    with pytest.raises(ValueError, match="same exercise"):
        production.submit_attempt(
            cloze["exercise_id"],
            "agent",
            "correct",
            "bad-revision",
            revision_of_attempt_id=partial["attempt_id"],
            now=NOW,
        )
    with pytest.raises(ValueError, match="different attempt"):
        production.submit_attempt(
            sentence["exercise_id"],
            "A different answer.",
            "correct",
            "sentence-1",
            now=NOW,
        )


def test_schema_v2_migrates_production_columns_and_preserves_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE word_senses (
            id TEXT PRIMARY KEY, lemma TEXT NOT NULL, normalized_lemma TEXT NOT NULL,
            part_of_speech TEXT NOT NULL, definition_en TEXT NOT NULL,
            definition_zh TEXT, frequency_rank INTEGER, source TEXT NOT NULL,
            source_sense_id TEXT NOT NULL, identity_derived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(source, source_sense_id)
        );
        CREATE TABLE user_knowledge_states (
            sense_id TEXT PRIMARY KEY REFERENCES word_senses(id),
            recognition_score REAL NOT NULL DEFAULT 0.0,
            recall_score REAL NOT NULL DEFAULT 0.0,
            recognition_evidence_count INTEGER NOT NULL DEFAULT 0,
            recall_evidence_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE knowledge_evidence (
            id TEXT PRIMARY KEY,
            sense_id TEXT NOT NULL REFERENCES word_senses(id),
            dimension TEXT NOT NULL CHECK (dimension IN ('recognition', 'recall')),
            value REAL NOT NULL,
            evidence_type TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(dimension, evidence_type, source_event_id)
        );
        INSERT INTO word_senses VALUES (
            'sense', 'agent', 'agent', 'noun', 'one that acts', '智能体', 900,
            'legacy', 'agent.n', 0, '2026-01-01', '2026-01-01'
        );
        INSERT INTO user_knowledge_states VALUES (
            'sense', 0.8, 0.5, 2, 1, '2026-01-01'
        );
        INSERT INTO knowledge_evidence VALUES (
            'evidence', 'sense', 'recognition', 0.8, 'review', 'event', '2026-01-01'
        );
        """
    )
    connection.commit()
    connection.close()

    database = LearningDatabase(path)
    database.initialize()

    with database.connect() as migrated:
        columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(user_knowledge_states)")
        }
        membership_columns = {
            row["name"]
            for row in migrated.execute("PRAGMA table_info(word_sense_collections)")
        }
        evidence = migrated.execute("SELECT * FROM knowledge_evidence").fetchall()
        migrated.execute(
            """
            INSERT INTO knowledge_evidence VALUES (
                'production', 'sense', 'production', 0.9,
                'production_attempt', 'attempt', '2026-01-02'
            )
            """
        )
    assert {"production_score", "production_evidence_count"} <= columns
    assert {"collection_id", "priority_rank", "sense_rank"} <= membership_columns
    assert len(evidence) == 1
    assert evidence[0]["id"] == "evidence"


def test_weekly_report_aggregates_events_without_creating_learning_rows(
    service: LearningService,
) -> None:
    document_id, sense_id = _accepted_reading(service)
    production = ProductionService(service.database)
    exercises = production.plan_for_document(document_id, now=NOW)["exercises"]
    cloze = next(item for item in exercises if item["exercise_type"] == "cloze")
    production.submit_attempt(
        cloze["exercise_id"], "wrong", None, "weekly-wrong", now=NOW
    )
    card = service.daily_plan(new_limit=0, review_limit=10, now=NOW)["reviews"][0]
    service.record_review(card["card_id"], "good", "weekly-review", now=NOW)

    with service.database.connect() as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "assessment_events",
                "review_events",
                "encounters",
                "production_attempts",
            )
        }
    report = ReportService(service.database).weekly_report(
        days=7, now=NOW + timedelta(days=1)
    )
    with service.database.connect() as connection:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }

    assert after == before
    assert report["reviews"]["attempts"] == 1
    assert report["reviews"]["ratings"]["good"] == 1
    assert report["reading"]["documents"] == 1
    assert report["reading"]["encounters"] == 2
    assert report["reading"]["encountered_senses"] == 1
    assert report["production"]["attempts"] == 1
    assert report["production"]["outcomes"]["incorrect"] == 1
    assert report["current_state"]["vocabulary_senses"] == 1
    assert report["current_state"]["due_reviews"] >= 1
    assert sense_id


def test_empty_weekly_report_has_stable_zero_categories(
    service: LearningService,
) -> None:
    report = ReportService(service.database).weekly_report(days=7, now=NOW)

    assert report["assessment"]["responses"] == {
        "known": 0,
        "unsure": 0,
        "unknown": 0,
    }
    assert report["reviews"]["ratings"] == {
        "again": 0,
        "hard": 0,
        "good": 0,
        "easy": 0,
    }
    assert report["production"]["outcomes"] == {
        "incorrect": 0,
        "partial": 0,
        "correct": 0,
    }


def test_vocabulary_search_returns_mastery_and_collection_metadata(
    service: LearningService,
) -> None:
    sense_id = service.upsert_vocabulary(
        VocabularyEntry.from_mapping(
            {
                "lemma": "derive",
                "part_of_speech": "verb",
                "definition_en": "obtain from a source",
                "frequency_rank": 900,
                "source": "fixture",
                "source_sense_id": "derive.v.obtain",
                "collection": {
                    "id": "general-test",
                    "title": "General Test",
                    "kind": "general",
                    "source": "fixture",
                    "version": "1",
                    "license": "test",
                    "rank": 12,
                    "sense_rank": 1,
                    "source_lemma": "derive",
                },
            }
        ),
        now=NOW,
    )["sense_id"]
    service.record_assessment(sense_id, "known", "1-1000", now=NOW)

    result = service.search_vocabulary("der", collection_id="general-test")

    assert result["count"] == 1
    assert result["items"][0]["lemma"] == "derive"
    assert result["items"][0]["collection_rank"] == 12
    assert result["items"][0]["recognition_score"] == 0.9


def test_cmudict_import_adds_ipa_respelling_and_variants(
    service: LearningService, tmp_path: Path
) -> None:
    for lemma, rank in (("derive", 900), ("record", 901)):
        service.upsert_vocabulary(
            VocabularyEntry(
                lemma=lemma,
                part_of_speech="verb",
                definition_en=f"definition for {lemma}",
                definition_zh=None,
                frequency_rank=rank,
                source="fixture",
                source_sense_id=f"{lemma}.v",
            ),
            now=NOW,
        )
    cmudict = tmp_path / "cmudict.dict"
    cmudict.write_text(
        ";;; fixture\n"
        "derive D IH0 R AY1 V\n"
        "record R EH1 K ER0 D\n"
        "record(2) R IH0 K AO1 R D\n"
        "ignored IH0 G N AO1 R D\n",
        encoding="utf-8",
    )
    pronunciations = PronunciationService(service.database)

    imported = pronunciations.import_cmudict(cmudict, source_version="fixture-1")
    repeated = pronunciations.import_cmudict(cmudict, source_version="fixture-1")
    derive = pronunciations.lookup("derive", part_of_speech="verb")
    record = pronunciations.lookup("record", part_of_speech="verb")

    assert imported == {"created": 3, "updated": 0, "skipped": 1, "errors": []}
    assert repeated == {"created": 0, "updated": 3, "skipped": 1, "errors": []}
    assert derive[0]["ipa"] == "dɪˈraɪv"
    assert derive[0]["respelling"] == "dih-RĪV"
    assert derive[0]["stress_pattern"] == "01"
    assert [item["ipa"] for item in record] == ["ˈrɛkɚd", "rɪˈkɔrd"]
    assert service.stats()["pronunciations"] == 3
    assert service.search_vocabulary("derive")["items"][0]["pronunciations"] == derive


@pytest.mark.parametrize(
    ("arpabet", "ipa", "respelling"),
    (
        ("D IH0 R AY1 V", "dɪˈraɪv", "dih-RĪV"),
        ("R EH1 K ER0 D", "ˈrɛkɚd", "REH-kerd"),
        ("IH0 K S T R IY1 M", "ɪkˈstriːm", "ihk-STRĒM"),
    ),
)
def test_arpabet_display_conversion(
    arpabet: str, ipa: str, respelling: str
) -> None:
    assert arpabet_to_ipa(arpabet) == ipa
    assert arpabet_to_respelling(arpabet) == respelling


def test_oewn_derivations_are_imported_as_source_aware_word_family(
    service: LearningService, tmp_path: Path
) -> None:
    service.upsert_vocabulary(
        VocabularyEntry(
            lemma="derive",
            part_of_speech="verb",
            definition_en="obtain from a source",
            definition_zh="从某来源获得",
            frequency_rank=900,
            source="oewn-2025",
            source_sense_id="derive%2:40:00::",
        ),
        now=NOW,
    )
    wordnet = _wordnet_fixture(tmp_path / "oewn-2025.zip")
    lexical = LexicalAnalysisService(service.database)

    imported = lexical.import_oewn_derivations(wordnet)
    repeated = lexical.import_oewn_derivations(wordnet)
    result = lexical.lookup(
        "derive",
        part_of_speech="verb",
        source_sense_id="derive%2:40:00::",
    )

    assert imported == {"created": 2, "updated": 0, "skipped": 0, "errors": []}
    assert repeated == {"created": 0, "updated": 2, "skipped": 0, "errors": []}
    assert {(item["form"], item["part_of_speech"]) for item in result["word_family"]} == {
        ("derivation", "noun"),
        ("derivative", "adjective"),
    }
    assert {item["source_level"] for item in result["word_family"]} == {
        "authoritative"
    }
    searched = service.search_vocabulary("derive")["items"][0]
    assert len(searched["lexical_analysis"]["word_family"]) == 2
    assert service.stats()["lexical_relations"] == 2


def test_llm_analysis_is_validated_cached_and_identified(
    service: LearningService,
) -> None:
    lexical = LexicalAnalysisService(service.database)
    payload = {
        "form": "unpredictable",
        "part_of_speech": "adjective",
        "analysis_type": "modern_morphology",
        "status": "available",
        "source_level": "llm_inferred",
        "content": {
            "segments": [
                {"form": "un-", "type": "prefix", "meaning": "not"},
                {"form": "predict", "type": "base", "meaning": "say in advance"},
                {"form": "-able", "type": "suffix", "meaning": "capable of"},
            ],
            "compositionality": "transparent",
        },
        "explanation_zh": "由否定前缀、词基和形容词后缀构成。",
        "confidence": 0.91,
        "model_name": "fixture-model",
        "prompt_version": "lexical-analysis-v1",
    }

    created = lexical.upsert_analysis(payload)
    updated = lexical.upsert_analysis(payload)
    result = lexical.lookup("unpredictable", part_of_speech="adjective")

    assert created["created"] is True
    assert updated["created"] is False
    assert result["needs_inference"]["modern_morphology"] is False
    assert result["needs_inference"]["historical_etymology"] is True
    assert result["analyses"][0]["source_level"] == "llm_inferred"
    assert result["analyses"][0]["content"]["segments"][1]["form"] == "predict"
    with pytest.raises(ValueError, match="model_name"):
        lexical.upsert_analysis({**payload, "model_name": None})


def test_lexical_inference_generates_both_types_once_and_caches_terminal_results(
    service: LearningService,
) -> None:
    sense_id = service.upsert_vocabulary(
        VocabularyEntry(
            "inspection", "noun", "careful examination", "检查", 2100,
            "fixture", "inspection.n",
        ),
        now=NOW,
    )["sense_id"]
    calls: list[dict] = []

    def generate(payload: dict) -> dict:
        calls.append(payload)
        assert payload["word"]["form"] == "inspection"
        return {
            "analyses": [
                {
                    "analysis_type": "modern_morphology",
                    "status": "available",
                    "content": {
                        "segments": [
                            {"form": "in-", "type": "prefix", "meaning": "into"},
                            {"form": "spect", "type": "root", "meaning": "look"},
                            {"form": "-ion", "type": "suffix", "meaning": "act or process"},
                        ],
                        "compositionality": "partly_transparent",
                    },
                    "explanation_zh": "现代学习中可按前缀、词根和名词后缀辅助记忆。",
                    "confidence": 0.88,
                },
                {
                    "analysis_type": "historical_etymology",
                    "status": "available",
                    "content": {
                        "summary_zh": "经法语进入英语，历史上与拉丁语中“查看”有关。",
                        "origin_language": "Latin via French",
                        "semantic_evolution": "查看 → 仔细检查",
                    },
                    "explanation_zh": "历史词源用于记忆，不表示现代词义能完全由词根推出。",
                    "confidence": 0.82,
                },
            ]
        }

    inference = LexicalInferenceService(service.database, generator=generate)
    first = inference.analyze_sense(sense_id)
    repeated = inference.analyze_sense(sense_id)

    assert len(calls) == 1
    assert first["generated_types"] == [
        "historical_etymology", "modern_morphology"
    ]
    assert repeated["generated_types"] == []
    assert repeated["cached"] is True
    assert repeated["lexical_analysis"]["needs_inference"] == {
        "historical_etymology": False,
        "modern_morphology": False,
    }


def test_low_confidence_etymology_is_cached_as_ambiguous(
    service: LearningService,
) -> None:
    sense_id = service.upsert_vocabulary(
        VocabularyEntry("study", "noun", "learning activity", "学习", 500,
                        "fixture", "study.n"),
        now=NOW,
    )["sense_id"]

    def generate(_payload: dict) -> dict:
        return {
            "analyses": [
                {
                    "analysis_type": "modern_morphology", "status": "opaque",
                    "content": {"summary_zh": "现代英语中不宜强行拆分。"},
                    "explanation_zh": "作为整体记忆。", "confidence": 0.9,
                },
                {
                    "analysis_type": "historical_etymology", "status": "available",
                    "content": {"summary_zh": "来源不确定。"},
                    "explanation_zh": "仅作可能解释。", "confidence": 0.5,
                },
            ]
        }

    result = LexicalInferenceService(
        service.database, generator=generate
    ).analyze_sense(sense_id)

    analyses = result["lexical_analysis"]["analyses"]
    assert {item["status"] for item in analyses} == {"opaque", "ambiguous"}
    assert all(not needed for needed in result["lexical_analysis"]["needs_inference"].values())


def test_lexical_inference_repairs_one_invalid_model_response(
    service: LearningService,
) -> None:
    sense_id = service.upsert_vocabulary(
        VocabularyEntry("inspect", "verb", "examine carefully", "检查", 1200,
                        "fixture", "inspect.v"),
        now=NOW,
    )["sense_id"]
    calls = 0

    def generate(payload: dict) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"analyses": [{"analysis_type": "modern_morphology"}]}
        assert "repair" in payload
        return {
            "analyses": [
                {"analysis_type": "historical_etymology", "status": "opaque",
                 "content": {"summary_zh": "不提供更多历史断言。"},
                 "explanation_zh": "资料不足。", "confidence": 0.8},
                {"analysis_type": "modern_morphology", "status": "opaque",
                 "content": {"summary_zh": "当前作为整体学习。"},
                 "explanation_zh": "不强行拆分。", "confidence": 0.8},
            ]
        }

    result = LexicalInferenceService(
        service.database, generator=generate
    ).analyze_sense(sense_id)

    assert calls == 2
    assert result["lexical_analysis"]["needs_inference"] == {
        "historical_etymology": False,
        "modern_morphology": False,
    }


def test_lexical_inference_normalizes_bounded_schema_variants(
    service: LearningService,
) -> None:
    sense_id = service.upsert_vocabulary(
        VocabularyEntry("study", "verb", "learn about a subject", "学习", 500,
                        "fixture", "study.v"),
        now=NOW,
    )["sense_id"]

    def generate(_payload: dict) -> dict:
        return {"analyses": [
            {"analysis_type": "historical_etymology", "status": "uncertain",
             "content": "历史路径存在多种解释。", "explanation_zh": "谨慎处理。",
             "confidence": 0.6},
            {"analysis_type": "modern_morphology", "status": "not_applicable",
             "summary_zh": "现代英语中作为整体使用。", "explanation_zh": "不强拆。",
             "confidence": 0.9},
        ]}

    result = LexicalInferenceService(
        service.database, generator=generate
    ).analyze_sense(sense_id)

    analyses = result["lexical_analysis"]["analyses"]
    assert {item["status"] for item in analyses} == {"ambiguous", "opaque"}
    assert all(not value for value in result["lexical_analysis"]["needs_inference"].values())


def test_learning_web_runs_lexical_inference_as_background_job(tmp_path: Path) -> None:
    database = tmp_path / "learning.db"
    service = LearningService(LearningDatabase(database))
    sense_id = service.upsert_vocabulary(
        VocabularyEntry("inspection", "noun", "careful examination", "检查", 2100,
                        "fixture", "inspection.n"),
        now=NOW,
    )["sense_id"]

    def generate(_payload: dict) -> dict:
        return {
            "analyses": [
                {
                    "analysis_type": "modern_morphology", "status": "available",
                    "content": {"segments": [
                        {"form": "inspect", "type": "base", "meaning": "examine"},
                        {"form": "-ion", "type": "suffix", "meaning": "process"},
                    ], "compositionality": "transparent"},
                    "explanation_zh": "inspect 加名词后缀。", "confidence": 0.9,
                },
                {
                    "analysis_type": "historical_etymology", "status": "opaque",
                    "content": {"summary_zh": "当前不提供未经核实的历史细节。"},
                    "explanation_zh": "没有足够可靠的细节。", "confidence": 0.8,
                },
            ]
        }

    app = create_app(
        database,
        session_token="test-session-token",
        lexical_generator=generate,
    )

    async def exercise_app():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={SESSION_HEADER: "test-session-token"},
        ) as client:
            queued = await client.post(
                "/api/lexical/inference", json={"sense_id": sense_id}
            )
            status = queued.json()
            for _ in range(100):
                if status["status"] in {"ready", "failed"}:
                    break
                await asyncio.sleep(0.01)
                status = (
                    await client.get(f"/api/lexical/inference/{status['job_id']}")
                ).json()
            return queued, status

    queued, status = asyncio.run(exercise_app())

    assert queued.status_code == 200
    assert status["status"] == "ready"
    assert status["result"]["lexical_analysis"]["needs_inference"] == {
        "historical_etymology": False,
        "modern_morphology": False,
    }


def test_learning_web_is_session_gated_and_host_restricted(tmp_path: Path) -> None:
    database = tmp_path / "learning.db"
    service = LearningService(LearningDatabase(database))
    seed(service, [100, 1100, 2100, 3100])
    app = create_app(database, session_token="test-session-token")

    async def exercise_app():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            return (
                await client.get("/"),
                await client.get("/api/stats"),
                await client.get(
                    "/api/stats", headers={SESSION_HEADER: "test-session-token"}
                ),
                await client.get(
                    "/api/health", headers={"Host": "attacker.example"}
                ),
                await client.post(
                    "/api/assessment/sample",
                    headers={SESSION_HEADER: "test-session-token"},
                    json={"per_band": 1, "seed": 3},
                ),
            )

    page, unauthorized, authorized, hostile_host, sampled = asyncio.run(
        exercise_app()
    )

    assert page.status_code == 200
    assert 'content="test-session-token"' in page.text
    assert page.headers["content-security-policy"].startswith("default-src 'self'")
    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json()["vocabulary_senses"] == 4
    assert hostile_host.status_code == 400
    assert sampled.status_code == 200
    assert sampled.json()["count"] == 4


def test_reading_tutor_uses_profile_targets_and_caches_generation(
    service: LearningService,
) -> None:
    target = service.upsert_vocabulary(
        VocabularyEntry(
            "evidence",
            "noun",
            "information that supports a conclusion",
            "证据",
            700,
            "fixture",
            "evidence.n",
        ),
        now=NOW,
    )["sense_id"]
    service.record_assessment(
        target, "unknown", "1-1000", event_id="evidence-gap", now=NOW
    )
    calls: list[dict] = []

    def generate(payload: dict) -> dict:
        calls.append(payload)
        assert payload["learner"]["target_items"][0]["lemma"] == "evidence"
        return {
            "title": "How evidence changes a model",
            "passage": "A useful model changes when new evidence challenges it.",
            "level": "B1",
            "why_this_passage": "练习 evidence，并识别因果关系。",
            "target_lemmas": ["evidence"],
            "questions": [
                {
                    "id": "q1",
                    "type": "main_idea",
                    "prompt": "What is the main idea?",
                    "answer": "Models should respond to evidence.",
                }
            ],
            "writing_prompt": "Summarize the passage in one sentence.",
        }

    tutor = ReadingTutorService(service.database, generator=generate)
    first = tutor.today(level="B1", minutes=10, topic="science", now=NOW)
    repeated = tutor.today(level="B1", minutes=10, topic="science", now=NOW)

    assert {**first, "cached": None} == {**repeated, "cached": None}
    assert len(calls) == 1
    assert first["source_mode"] == "source_adapted"
    assert first["target_items"][0]["sense_id"] == target
    assert first["source"]["url"].startswith("https://")
    assert first["cached"] is False
    assert repeated["cached"] is True


def test_reading_tutor_falls_back_to_curated_seed_when_generation_fails(
    service: LearningService,
) -> None:
    def fail(_payload: dict) -> dict:
        raise RuntimeError("model unavailable")

    result = ReadingTutorService(service.database, generator=fail).today(
        level="B1", minutes=10, topic="science", now=NOW
    )

    assert result["source_mode"] == "curated_seed"
    assert result["generation_status"] == "fallback"
    assert len(result["passage"].split()) >= 80
    assert result["questions"]
    assert "model unavailable" not in json.dumps(result)


def test_learning_web_serves_proactive_reading_and_distinct_review_front_style(
    tmp_path: Path,
) -> None:
    database = tmp_path / "learning.db"
    generated = {
        "title": "A short lesson",
        "passage": "Careful readers compare a claim with its evidence.",
        "level": "B1",
        "why_this_passage": "练习学术阅读中的证据判断。",
        "target_lemmas": [],
        "questions": [
            {
                "id": "q1",
                "type": "main_idea",
                "prompt": "What do careful readers compare?",
                "answer": "A claim and its evidence.",
            }
        ],
        "writing_prompt": "Write one sentence about the passage.",
    }
    app = create_app(
        database,
        session_token="test-session-token",
        reading_generator=lambda _payload: generated,
    )

    async def exercise_app():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={SESSION_HEADER: "test-session-token"},
        ) as client:
            return await client.get("/"), await client.post(
                "/api/reading/today",
                json={"level": "B1", "minutes": 10, "topic": "science"},
            )

    page, reading = asyncio.run(exercise_app())

    assert reading.status_code == 200
    assert reading.json()["source_mode"] == "source_adapted"
    assert 'data-view="reading"' in page.text
    assert 'id="review-front" class="review-front' in page.text


def test_writing_coach_persists_feedback_and_links_revision(
    service: LearningService,
) -> None:
    lesson = ReadingTutorService(service.database, generator=None).today(
        level="B1", minutes=10, topic="science", now=NOW
    )
    calls: list[dict] = []

    def review(payload: dict) -> dict:
        calls.append(payload)
        is_revision = payload["submission"]["stage"] == "revision"
        return {
            "summary_zh": "修改稿更清楚。" if is_revision else "主旨基本清楚。",
            "strengths": ["抓住了模型需要根据证据调整这一点。"],
            "issues": [] if is_revision else [
                {
                    "category": "grammar",
                    "excerpt": "evidence are",
                    "explanation_zh": "evidence 在这里是不可数名词。",
                    "hint_zh": "检查谓语的单复数形式。",
                }
            ],
            "revision_priorities": [] if is_revision else ["修正主谓一致"],
            "traits": {
                "content": "覆盖了文章主旨。",
                "accuracy": "有一处主谓一致问题。" if not is_revision else "表达准确。",
                "cohesion": "句间关系清楚。",
                "register": "语域适合简短学术摘要。",
            },
        }

    coach = WritingCoachService(service.database, generator=review)
    first = coach.review(
        lesson["lesson_id"],
        "The evidence are important, so a model should change.",
        "writing-1",
        now=NOW,
    )
    revision = coach.review(
        lesson["lesson_id"],
        "The evidence is important, so a model should change.",
        "writing-2",
        parent_submission_id=first["submission_id"],
        now=NOW + timedelta(minutes=5),
    )
    duplicate = coach.review(
        lesson["lesson_id"],
        "The evidence is important, so a model should change.",
        "writing-2",
        parent_submission_id=first["submission_id"],
        now=NOW + timedelta(minutes=6),
    )

    assert first["stage"] == "initial"
    assert first["feedback"]["issues"][0]["category"] == "grammar"
    assert revision["stage"] == "revision"
    assert revision["parent_submission_id"] == first["submission_id"]
    assert duplicate["duplicate"] is True
    assert len(calls) == 2


def test_learning_items_group_cards_by_concrete_sense(service: LearningService) -> None:
    sense_id = service.upsert_vocabulary(
        VocabularyEntry(
            "evidence", "noun", "supporting information", "证据", 700,
            "fixture", "evidence.n",
        ),
        now=NOW,
    )["sense_id"]
    service.record_assessment(
        sense_id, "unknown", "1-1000", event_id="gap", now=NOW
    )
    plan = service.daily_plan(new_limit=1, review_limit=0, now=NOW)
    service.record_review(
        plan["new_items"][0]["card_id"], "good", "review-one", now=NOW
    )

    notebook = service.learning_items(limit=20)

    assert notebook["count"] == 1
    assert notebook["items"][0]["sense_id"] == sense_id
    assert notebook["items"][0]["lemma"] == "evidence"
    assert notebook["items"][0]["card_types"] == ["recognition"]
    assert notebook["items"][0]["next_review_at"] is not None


def test_vocabulary_search_item_can_be_added_to_notebook_idempotently(
    service: LearningService,
) -> None:
    sense_id = service.upsert_vocabulary(
        VocabularyEntry(
            "derive", "verb", "obtain from a source", "源自", 900,
            "fixture", "derive.v",
        ),
        now=NOW,
    )["sense_id"]

    first = service.add_learning_item(sense_id, now=NOW)
    repeated = service.add_learning_item(sense_id, now=NOW)

    assert first["created"] is True
    assert repeated["created"] is False
    assert first["card_id"] == repeated["card_id"]
    assert service.learning_items()["items"][0]["sense_id"] == sense_id


def test_learning_web_exposes_writing_review_and_separate_word_views(
    tmp_path: Path,
) -> None:
    database = tmp_path / "learning.db"
    feedback = {
        "summary_zh": "主旨清楚。",
        "strengths": ["表达简洁。"],
        "issues": [],
        "revision_priorities": [],
        "traits": {
            "content": "覆盖主旨。", "accuracy": "准确。",
            "cohesion": "连贯。", "register": "语域合适。",
        },
    }
    app = create_app(
        database,
        session_token="test-session-token",
        writing_generator=lambda _payload: feedback,
    )
    lesson = ReadingTutorService(
        LearningDatabase(database), generator=None
    ).today(level="B1", minutes=10, topic="science", now=NOW)

    async def exercise_app():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={SESSION_HEADER: "test-session-token"},
        ) as client:
            return await client.get("/"), await client.post(
                "/api/writing/review",
                json={
                    "lesson_id": lesson["lesson_id"],
                    "text": "A model should change when evidence changes.",
                    "idempotency_key": "web-writing-1",
                },
            ), await client.get("/api/vocabulary/notebook")

    page, reviewed, notebook = asyncio.run(exercise_app())

    assert reviewed.status_code == 200
    assert reviewed.json()["feedback"]["summary_zh"] == "主旨清楚。"
    assert notebook.status_code == 200
    assert 'data-view="learn"' in page.text
    assert 'data-view="review"' in page.text
    assert 'data-view="notebook"' in page.text


def test_learning_web_refuses_public_bind_without_touching_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "learning.db"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "learning_web.py"),
            "--host",
            "0.0.0.0",
            "--db",
            str(database),
        ],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "Refusing non-loopback bind" in completed.stderr
    assert not database.exists()


def test_skill_blueprint_is_daily_profile_local_terminal_automation() -> None:
    spec = parse_blueprint((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))

    assert spec is not None
    assert spec.skill_name == "personal-english-learning"
    assert spec.schedule == "0 8 * * *"
    assert spec.deliver == "origin"
    assert spec.enabled_toolsets == ["terminal"]
    assert "daily-plan" in spec.prompt
    assert "weekly-report --days 7" in spec.prompt
    job = blueprint_to_job_spec(spec)
    assert job["skills"] == ["personal-english-learning"]
    assert job["enabled_toolsets"] == ["terminal"]


def test_general_vocabulary_builder_is_deterministic_and_ranks_across_pos(
    tmp_path: Path,
) -> None:
    wordnet = _wordnet_fixture(tmp_path / "oewn-2025.zip")
    frequency = tmp_path / "frequency.csv"
    frequency.write_text(
        "rank,lemma\n1,the\n2,address\n3,derive\n4,123\n",
        encoding="utf-8",
    )
    ranked = load_ranked_lemmas(frequency)
    output = tmp_path / "general.jsonl"
    builder = VocabularyBuilder(wordnet)
    collection = CollectionSpec(
        "general-core-test",
        "General Core Test",
        "general",
        "fixture-frequency",
        "1",
        "test-only",
    )

    first = builder.build(
        ranked,
        output,
        collection,
        frequency_ranks=frequency_rank_map(ranked),
        lemma_limit=2,
        max_senses_per_lemma=2,
        ranked_source_path=frequency,
        frequency_source_path=frequency,
    )
    first_output = output.read_bytes()
    first_manifest = Path(first["manifest"]).read_bytes()
    repeated = builder.build(
        ranked,
        output,
        collection,
        frequency_ranks=frequency_rank_map(ranked),
        lemma_limit=2,
        max_senses_per_lemma=2,
        force=True,
        ranked_source_path=frequency,
        frequency_source_path=frequency,
    )
    rows = [json.loads(line) for line in output.read_text().splitlines()]

    assert output.read_bytes() == first_output
    assert Path(repeated["manifest"]).read_bytes() == first_manifest
    assert first["selected_lemmas"] == 2
    assert first["sense_entries"] == 3
    assert first["unmatched_lemmas"] == ["the"]
    assert [(row["lemma"], row["part_of_speech"]) for row in rows] == [
        ("address", "verb"),
        ("address", "noun"),
        ("derive", "verb"),
    ]
    assert {row["source"] for row in rows} == {"oewn-2025"}
    assert [row["frequency_rank"] for row in rows] == [2, 2, 3]
    assert [row["collection"]["rank"] for row in rows] == [2, 2, 3]

    service = LearningService(LearningDatabase(tmp_path / "general.db"))
    service.import_jsonl(output)
    plan = service.daily_plan(
        new_limit=3,
        review_limit=0,
        now=NOW,
    )
    assert plan["collection_id"] == "general-core-test"
    assert plan["collection_selection"] == "auto_general"
    assert [item["lemma"] for item in plan["new_items"]] == ["address", "derive"]
    assert [item["collection_sense_rank"] for item in plan["new_items"]] == [1, 1]

    sample = service.assessment_sample(
        per_band=5,
        bands=((1, 10),),
        collection_id="general-core-test",
        seed=2,
    )
    assert sample["count"] == 2
    assert {item["lemma"] for item in sample["items"]} == {"address", "derive"}
    assert {item["collection_sense_rank"] for item in sample["items"]} == {1}


def test_academic_avl_adjective_code_is_supported() -> None:
    assert normalize_part_of_speech("j") == "adjective"


def test_academic_collection_reuses_senses_and_preserves_general_frequency(
    tmp_path: Path,
) -> None:
    wordnet = _wordnet_fixture(tmp_path / "oewn-2025.zip")
    frequency = tmp_path / "frequency.csv"
    frequency.write_text("rank,lemma\n10,address\n20,derive\n", encoding="utf-8")
    academic = tmp_path / "academic.csv"
    academic.write_text(
        "lemma,academic_rank,pos\naddress,1,verb\nderive,2,verb\n",
        encoding="utf-8",
    )
    general_output = tmp_path / "general.jsonl"
    academic_output = tmp_path / "academic.jsonl"
    builder = VocabularyBuilder(wordnet)
    frequency_items = load_ranked_lemmas(frequency)
    builder.build(
        frequency_items,
        general_output,
        CollectionSpec("general", "General", "general", "fixture", "1", "test"),
        frequency_ranks=frequency_rank_map(frequency_items),
        lemma_limit=2,
        max_senses_per_lemma=2,
    )
    builder.build(
        load_ranked_lemmas(academic),
        academic_output,
        CollectionSpec("academic", "Academic", "academic", "fixture", "1", "test"),
        lemma_limit=2,
        max_senses_per_lemma=1,
    )
    service = LearningService(LearningDatabase(tmp_path / "learning.db"))

    service.import_jsonl(general_output)
    service.import_jsonl(academic_output)

    with service.database.connect() as connection:
        senses = connection.execute(
            "SELECT lemma, part_of_speech, frequency_rank FROM word_senses"
        ).fetchall()
        memberships = connection.execute(
            "SELECT collection_id, priority_rank FROM word_sense_collections"
        ).fetchall()
    assert len(senses) == 3
    assert next(
        row["frequency_rank"]
        for row in senses
        if row["lemma"] == "address" and row["part_of_speech"] == "verb"
    ) == 10
    assert len(memberships) == 5
    assert {row["collection_id"] for row in memberships} == {"general", "academic"}

    sample = service.assessment_sample(
        per_band=5,
        bands=((1, 10),),
        collection_id="academic",
        seed=2,
    )
    assert {item["lemma"] for item in sample["items"]} == {"address", "derive"}
    assert {item["collection_rank"] for item in sample["items"]} == {1, 2}
    plan = service.daily_plan(
        new_limit=2,
        review_limit=0,
        collection_id="academic",
        now=NOW,
    )
    assert [item["lemma"] for item in plan["new_items"]] == ["address", "derive"]
    assert [item["collection_rank"] for item in plan["new_items"]] == [1, 2]
    assert [item["kind"] for item in service.stats()["collections"]] == [
        "academic",
        "general",
    ]


def test_vocabulary_builder_cli_creates_importable_general_collection(
    tmp_path: Path,
) -> None:
    wordnet = _wordnet_fixture(tmp_path / "oewn-2025.zip")
    frequency = tmp_path / "frequency.tsv"
    frequency.write_text(
        "rank\tword\n1\ta\n2\taddress\n3\tderive\n", encoding="utf-8"
    )
    output = tmp_path / "built.jsonl"
    database = tmp_path / "learning.db"

    built = subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "vocabulary-build-general",
            "--wordnet-zip",
            str(wordnet),
            "--sense-index-zip",
            str(wordnet),
            "--frequency-list",
            str(frequency),
            "--output",
            str(output),
            "--limit",
            "2",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    imported = subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "--db",
            str(database),
            "import-jsonl",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(built.stdout)["selected_lemmas"] == 2
    assert json.loads(imported.stdout)["created"] == 3
    assert {
        json.loads(line)["lemma"] for line in output.read_text().splitlines()
    } == {"address", "derive"}
    assert output.with_suffix(".jsonl.manifest.json").is_file()


def test_academic_builder_cli_does_not_backfill_beyond_rank_limit(
    tmp_path: Path,
) -> None:
    wordnet = _wordnet_fixture(tmp_path / "oewn-2025.zip")
    academic = tmp_path / "academic.csv"
    academic.write_text(
        "academic_rank,lemma,pos\n1,missing,n\n2,address,v\n3,derive,v\n",
        encoding="utf-8",
    )
    output = tmp_path / "academic.jsonl"

    completed = subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "vocabulary-build-academic",
            "--wordnet-zip",
            str(wordnet),
            "--sense-index-zip",
            str(wordnet),
            "--academic-list",
            str(academic),
            "--output",
            str(output),
            "--limit",
            "2",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = [json.loads(line) for line in output.read_text().splitlines()]

    assert json.loads(completed.stdout)["selected_lemmas"] == 1
    assert {row["lemma"] for row in rows} == {"address"}


def test_toefl_2026_profile_preserves_version_sources_and_task_contract() -> None:
    profile = ExamService().profile()

    assert profile["exam_id"] == "toefl_ibt"
    assert profile["profile_version"] == "2026.1"
    assert profile["effective_from"] == "2026-01-21"
    assert profile["status"] == "active"
    assert profile["official"] is False
    assert {source["publisher"] for source in profile["sources"]} == {"ETS"}
    assert profile["sections"]["reading"]["delivery"] == "two_stage_adaptive"
    assert profile["sections"]["listening"]["delivery"] == "two_stage_adaptive"
    assert profile["sections"]["writing"]["delivery"] == "linear"
    assert profile["sections"]["speaking"]["delivery"] == "linear"
    assert [
        task["id"] for task in profile["sections"]["reading"]["task_types"]
    ] == [
        "complete_the_words",
        "read_in_daily_life",
        "read_an_academic_passage",
    ]


def test_toefl_score_uses_decimal_half_up_contract_and_comparison_range() -> None:
    exam = ExamService()

    rounds_down = exam.calculate_score(
        {"reading": "5.0", "listening": "5.0", "speaking": "5.0", "writing": "5.5"}
    )
    rounds_up = exam.calculate_score(
        {"reading": "5.0", "listening": "5.0", "speaking": "5.5", "writing": "5.5"}
    )

    assert rounds_down["section_mean"] == "5.125"
    assert rounds_down["overall"] == "5.0"
    assert rounds_down["legacy_comparable_total_range"] == {
        "minimum": 95,
        "maximum": 106,
    }
    assert rounds_down["legacy_mapping_kind"] == "comparison_range_not_exact_conversion"
    assert rounds_up["section_mean"] == "5.25"
    assert rounds_up["overall"] == "5.5"
    assert "not an official ETS score prediction" in rounds_up["disclaimer"]


@pytest.mark.parametrize(
    "scores, message",
    [
        ({"reading": "5.0"}, "must contain"),
        (
            {"reading": "4.2", "listening": "4.0", "speaking": "4.0", "writing": "4.0"},
            "increments",
        ),
        (
            {"reading": "6.5", "listening": "4.0", "speaking": "4.0", "writing": "4.0"},
            "between",
        ),
    ],
)
def test_toefl_score_rejects_invalid_section_bands(
    scores: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ExamService().calculate_score(scores)


def test_toefl_practice_plan_is_bounded_balanced_and_not_adaptive_simulation() -> None:
    plan = ExamService().practice_plan(minutes=30)

    assert plan["scheduled_minutes"] <= plan["requested_minutes"]
    assert plan["remaining_minutes"] == 3
    assert plan["sections"] == ["reading", "writing"]
    assert {task["section"] for task in plan["tasks"]} == {"reading", "writing"}
    assert [task["task_type"] for task in plan["tasks"]] == [
        "complete_the_words",
        "build_a_sentence",
        "read_in_daily_life",
        "write_an_email",
    ]
    assert plan["adaptive_simulation"] is False

    with pytest.raises(ValueError, match="not implemented"):
        ExamService().practice_plan(minutes=30, sections=["listening"])


def test_toefl_cli_works_without_creating_learning_database(tmp_path: Path) -> None:
    database = tmp_path / "unused" / "learning.db"

    def invoke(*arguments: str) -> dict:
        completed = subprocess.run(
            [sys.executable, str(CLI_PATH), "--db", str(database), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    profile = invoke("toefl-profile")
    score = invoke(
        "toefl-score",
        "--reading",
        "5.0",
        "--listening",
        "5.0",
        "--speaking",
        "5.0",
        "--writing",
        "5.5",
    )
    plan = invoke(
        "toefl-practice-plan",
        "--minutes",
        "20",
        "--section",
        "writing",
    )

    assert profile["profile"]["profile_version"] == "2026.1"
    assert score["overall"] == "5.0"
    assert plan["sections"] == ["writing"]
    assert {task["section"] for task in plan["tasks"]} == {"writing"}
    assert not database.exists()


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
    added = invoke(
        "add-sense",
        "--lemma",
        "agent",
        "--part-of-speech",
        "noun",
        "--definition-en",
        "a system that acts",
        "--definition-zh",
        "智能体",
        "--frequency-rank",
        "900",
        "--source",
        "e2e",
        "--source-sense-id",
        "agent.n",
    )
    reading = invoke(
        "reading-analyze",
        "--text",
        "An agent can help another agent.",
        "--title",
        "Agents",
    )
    confirmed = invoke(
        "reading-confirm",
        "--document-id",
        reading["document_id"],
        "--decision",
        f"{added['sense_id']}=accepted",
    )
    production_plan = invoke(
        "production-plan", "--document-id", reading["document_id"]
    )
    cloze = next(
        item
        for item in production_plan["exercises"]
        if item["exercise_type"] == "cloze"
    )
    production_attempt = invoke(
        "production-submit",
        "--exercise-id",
        cloze["exercise_id"],
        "--answer-text",
        cloze["expected_answer"],
        "--idempotency-key",
        "e2e-production",
    )
    stats = invoke("stats")
    weekly = invoke("weekly-report", "--days", "7")

    assert imported["created"] == 4
    assert sampled["count"] == 4
    assert assessed["duplicate"] is False
    assert reviewed["interval_seconds"] > 0
    assert reading["targets"][0]["sense_id"] == added["sense_id"]
    assert confirmed["encounters_created"] == 2
    assert production_attempt["outcome"] == "correct"
    assert stats["vocabulary_senses"] == 5
    assert stats["assessed_senses"] == 1
    assert stats["mean_production"] > 0
    assert weekly["reading"]["documents"] == 1
    assert weekly["production"]["outcomes"]["correct"] == 1
    assert database.exists()
