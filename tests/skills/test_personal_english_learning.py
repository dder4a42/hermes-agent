"""Behavior tests for the personal English learning optional skill."""

from __future__ import annotations

import json
import sqlite3
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

from learning_core import (
    ExamService,
    LearningDatabase,
    LearningService,
    ProductionService,
    ReadingService,
    ReportService,
    VocabularyEntry,
)
from learning_core.reading import lemma_candidates
from tools.blueprints import blueprint_to_job_spec, parse_blueprint


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

    assert first == repeated
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
