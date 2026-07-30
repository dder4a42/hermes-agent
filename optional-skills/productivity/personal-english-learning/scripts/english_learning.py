#!/usr/bin/env python3
"""JSON CLI for the personal English learning SQLite service."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from learning_core import (
    ExamService,
    LearningDatabase,
    LearningService,
    ProductionService,
    ReadingService,
    ReportService,
    VocabularyEntry,
)


def default_database_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    return hermes_home / "personal-english-learning" / "learning.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_database_path())
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="Initialize the SQLite database")

    import_parser = commands.add_parser("import-jsonl", help="Import word senses")
    import_parser.add_argument("path", type=Path)

    add = commands.add_parser("add-sense", help="Add or update one word sense")
    add.add_argument("--lemma", required=True)
    add.add_argument("--part-of-speech", required=True)
    add.add_argument("--definition-en", required=True)
    add.add_argument("--definition-zh")
    add.add_argument("--frequency-rank", type=int)
    add.add_argument("--source", required=True)
    add.add_argument("--source-sense-id")

    sample = commands.add_parser("assessment-sample", help="Sample frequency bands")
    sample.add_argument("--per-band", type=int, default=10)
    sample.add_argument("--seed", type=int, default=0)

    assessment = commands.add_parser("assessment-record", help="Record one assessment")
    assessment.add_argument("--sense-id", required=True)
    assessment.add_argument("--response", choices=("known", "unsure", "unknown"), required=True)
    assessment.add_argument("--frequency-band", required=True)
    assessment.add_argument("--event-id")

    plan = commands.add_parser("daily-plan", help="Return due reviews and new senses")
    plan.add_argument("--review-limit", type=int, default=30)
    plan.add_argument("--new-limit", type=int, default=8)
    plan.add_argument("--backlog-reduce-at", type=int, default=30)
    plan.add_argument("--backlog-stop-at", type=int, default=60)

    review = commands.add_parser("review", help="Record and schedule one review")
    review.add_argument("--card-id", required=True)
    review.add_argument("--rating", choices=("again", "hard", "good", "easy"), required=True)
    review.add_argument("--idempotency-key", required=True)
    review.add_argument("--response-time-ms", type=int)
    review.add_argument("--hint-count", type=int, default=0)
    review.add_argument("--answer-text")

    reading = commands.add_parser("reading-analyze", help="Analyze a short reading")
    reading_input = reading.add_mutually_exclusive_group(required=True)
    reading_input.add_argument("--file", type=Path)
    reading_input.add_argument("--text")
    reading.add_argument("--title", default="Untitled reading")
    reading.add_argument("--source", default="user_text")
    reading.add_argument("--target-limit", type=int, default=5)

    confirm = commands.add_parser(
        "reading-confirm", help="Accept or reject reading target senses"
    )
    confirm.add_argument("--document-id", required=True)
    confirm.add_argument(
        "--decision",
        action="append",
        required=True,
        metavar="SENSE_ID=accepted|rejected",
    )

    production = commands.add_parser(
        "production-plan", help="Generate cloze and short-sentence exercises"
    )
    production.add_argument("--document-id", required=True)
    production.add_argument("--limit", type=int, default=6)

    submit = commands.add_parser(
        "production-submit", help="Record a production answer or revision"
    )
    submit.add_argument("--exercise-id", required=True)
    submit.add_argument("--answer-text", required=True)
    submit.add_argument("--outcome", choices=("correct", "partial", "incorrect"))
    submit.add_argument("--feedback")
    submit.add_argument("--revision-of-attempt-id")
    submit.add_argument("--idempotency-key", required=True)

    weekly = commands.add_parser(
        "weekly-report", help="Aggregate recent learning activity without mutation"
    )
    weekly.add_argument("--days", type=int, default=7)

    commands.add_parser(
        "toefl-profile", help="Show the versioned TOEFL 2026 practice profile"
    )

    score = commands.add_parser(
        "toefl-score", help="Calculate a 1-6 practice score from section bands"
    )
    for section in ("reading", "listening", "speaking", "writing"):
        score.add_argument(f"--{section}", required=True)

    toefl_plan = commands.add_parser(
        "toefl-practice-plan", help="Build a deterministic Reading/Writing plan"
    )
    toefl_plan.add_argument("--minutes", type=int, default=30)
    toefl_plan.add_argument(
        "--section",
        action="append",
        choices=("reading", "listening", "speaking", "writing"),
        dest="sections",
    )

    commands.add_parser("stats", help="Show learning progress statistics")
    return parser


def run(args: argparse.Namespace) -> dict:
    if args.command == "toefl-profile":
        return {"profile": ExamService().profile()}
    if args.command == "toefl-score":
        return ExamService().calculate_score(
            {
                "reading": args.reading,
                "listening": args.listening,
                "speaking": args.speaking,
                "writing": args.writing,
            }
        )
    if args.command == "toefl-practice-plan":
        sections = args.sections or ["reading", "writing"]
        return ExamService().practice_plan(minutes=args.minutes, sections=sections)

    service = LearningService(LearningDatabase(args.db))
    if args.command == "init":
        return {"ok": True, "database": str(args.db)}
    if args.command == "import-jsonl":
        return service.import_jsonl(args.path)
    if args.command == "add-sense":
        return service.upsert_vocabulary(
            VocabularyEntry(
                lemma=args.lemma,
                part_of_speech=args.part_of_speech,
                definition_en=args.definition_en,
                definition_zh=args.definition_zh,
                frequency_rank=args.frequency_rank,
                source=args.source,
                source_sense_id=args.source_sense_id,
            )
        )
    if args.command == "assessment-sample":
        return service.assessment_sample(per_band=args.per_band, seed=args.seed)
    if args.command == "assessment-record":
        return service.record_assessment(
            args.sense_id,
            args.response,
            args.frequency_band,
            event_id=args.event_id,
        )
    if args.command == "daily-plan":
        return service.daily_plan(
            review_limit=args.review_limit,
            new_limit=args.new_limit,
            backlog_reduce_at=args.backlog_reduce_at,
            backlog_stop_at=args.backlog_stop_at,
        )
    if args.command == "review":
        return service.record_review(
            args.card_id,
            args.rating,
            args.idempotency_key,
            response_time_ms=args.response_time_ms,
            hint_count=args.hint_count,
            answer_text=args.answer_text,
        )
    if args.command == "reading-analyze":
        content = args.text
        if args.file is not None:
            content = args.file.expanduser().read_text(encoding="utf-8")
        return ReadingService(service.database).analyze_text(
            content,
            title=args.title,
            source=args.source,
            target_limit=args.target_limit,
        )
    if args.command == "reading-confirm":
        decisions = []
        for raw in args.decision:
            sense_id, separator, status = raw.partition("=")
            if not separator:
                raise ValueError(
                    "decision must use SENSE_ID=accepted or SENSE_ID=rejected"
                )
            decisions.append({"sense_id": sense_id, "status": status})
        return ReadingService(service.database).confirm_targets(
            args.document_id, decisions
        )
    if args.command == "production-plan":
        return ProductionService(service.database).plan_for_document(
            args.document_id, limit=args.limit
        )
    if args.command == "production-submit":
        return ProductionService(service.database).submit_attempt(
            args.exercise_id,
            args.answer_text,
            args.outcome,
            args.idempotency_key,
            feedback=args.feedback,
            revision_of_attempt_id=args.revision_of_attempt_id,
        )
    if args.command == "weekly-report":
        return ReportService(service.database).weekly_report(days=args.days)
    if args.command == "stats":
        return service.stats()
    raise AssertionError(f"unhandled command: {args.command}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = run(args)
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
