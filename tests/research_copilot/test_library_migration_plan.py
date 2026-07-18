from __future__ import annotations

import json

from research_copilot.migration import build_migration_plan, render_migration_plan


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_plan_selects_only_recommendation_and_feedback_references(tmp_path):
    _write_jsonl(tmp_path / "candidates.jsonl", [
        {"id": "keep-rec"}, {"id": "keep-feedback"}, {"id": "archive"},
    ])
    _write_jsonl(tmp_path / "recommendations.jsonl", [{"id": "keep-rec"}])
    _write_jsonl(tmp_path / "interactions.jsonl", [{"item_id": "keep-feedback"}])
    plan = build_migration_plan(tmp_path)
    assert plan.selected_candidate_ids == ("keep-feedback", "keep-rec")
    assert plan.archive_candidate_count == 1
    assert plan.can_apply is True
    assert "Apply readiness: ready" in render_migration_plan(plan)


def test_plan_blocks_missing_references_and_duplicate_ids(tmp_path):
    _write_jsonl(tmp_path / "candidates.jsonl", [{"id": "dup"}, {"id": "dup"}])
    _write_jsonl(tmp_path / "recommendations.jsonl", [{"id": "missing"}])
    _write_jsonl(tmp_path / "interactions.jsonl", [])
    plan = build_migration_plan(tmp_path)
    assert plan.missing_candidate_ids == ("missing",)
    assert plan.duplicate_candidate_ids == ("dup",)
    assert plan.can_apply is False
    rendered = render_migration_plan(plan)
    assert "Missing ids: missing" in rendered
    assert "Duplicate ids: dup" in rendered


def test_plan_reports_corrupt_jsonl_without_mutating_files(tmp_path):
    candidate_path = tmp_path / "candidates.jsonl"
    candidate_path.write_text('{"id":"ok"}\nnot json\n')
    _write_jsonl(tmp_path / "recommendations.jsonl", [])
    _write_jsonl(tmp_path / "interactions.jsonl", [])
    before = candidate_path.read_bytes()
    plan = build_migration_plan(tmp_path)
    assert plan.invalid_candidate_lines == (2,)
    assert candidate_path.read_bytes() == before
