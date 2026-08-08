from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from research_copilot.evidence import (
    build_profile_update_proposal,
    create_evidence,
    write_profile_update_proposal,
)
from research_copilot.library import (
    LibraryRepository,
    ResearchItemDraft,
    SourceEvidence,
    connect_library,
    initialize_library,
)
from research_copilot.preferences import load_research_preferences


NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def _setup(tmp_path):
    topics = tmp_path / "topics.yaml"
    profile = tmp_path / "research-profile.yaml"
    topics.write_text(yaml.safe_dump({
        "schema_version": 2,
        "topics": [{
            "id": "agent", "status": "active", "search_queries": ["agent"],
            "match_terms": ["agent"], "exclude_terms": [],
        }],
    }), encoding="utf-8")
    profile.write_text(yaml.safe_dump({
        "schema_version": 2,
        "long_term_agenda": [
            {
                "id": "agenda-a", "topic_ids": ["agent"],
                "current_beliefs": [{
                    "id": "belief-a", "statement": "Explicit state improves reliability",
                    "confidence": 0.8, "updated_at": "2026-08-08",
                }],
                "open_questions": [{"id": "question-a", "question": "How much state is enough?"}],
            },
            {
                "id": "agenda-b", "topic_ids": ["agent"],
                "open_questions": [{"id": "question-b", "question": "How should recovery work?"}],
            },
        ],
        "evidence_ledger": [],
    }, sort_keys=False), encoding="utf-8")
    connection = connect_library(tmp_path / "library.db")
    initialize_library(connection, migrated_at=NOW.isoformat())
    repository = LibraryRepository(connection)
    repository.upsert_source(
        source_id="paper", provider="test", display_name="Paper",
        source_type="paper", tier=1.0, now=NOW,
    )
    item = repository.upsert_item(
        ResearchItemDraft(title="Reliable Agents", url="https://example.com/paper"),
        source=SourceEvidence("paper"), discovered_at=NOW,
    )
    preferences = load_research_preferences(topics, profile)
    return connection, repository, preferences, profile, item.item_id


def test_evidence_requires_reading_item_and_valid_same_agenda_scope(tmp_path):
    connection, repository, preferences, profile, item_id = _setup(tmp_path)
    try:
        with pytest.raises(ValueError, match="user-read or agent deep-researched"):
            create_evidence(
                repository, preferences, item_id=item_id, belief_id="belief-a",
                prompt_id=None, relation="supports", claim_type="source_claim",
                strength=0.8, source_quality="primary", claim="A", rationale="",
                profile_path=profile, created_at=NOW,
            )
        repository.record_feedback(item_id, kind="read", created_at=NOW)
        with pytest.raises(ValueError, match="same agenda"):
            create_evidence(
                repository, preferences, item_id=item_id, belief_id="belief-a",
                prompt_id="question-b", relation="supports", claim_type="source_claim",
                strength=0.8, source_quality="primary", claim="A", rationale="",
                profile_path=profile, created_at=NOW,
            )
    finally:
        connection.close()


def test_reviewed_evidence_builds_non_mutating_profile_proposal(tmp_path):
    connection, repository, preferences, profile, item_id = _setup(tmp_path)
    before = profile.read_bytes()
    try:
        repository.record_feedback(item_id, kind="read", created_at=NOW)
        evidence_id = create_evidence(
            repository, preferences, item_id=item_id, belief_id="belief-a",
            prompt_id="question-a", relation="challenges", claim_type="source_claim",
            strength=0.5, source_quality="primary",
            claim="The controlled result weakens the state hypothesis.",
            rationale="Primary experiment", profile_path=profile, created_at=NOW,
        )
        assert repository.list_evidence()[0]["review_status"] == "pending"
        repository.review_evidence(
            evidence_id, accepted=True, reviewed_at=NOW, note="checked paper",
        )
        with pytest.raises(ValueError, match="already reviewed"):
            repository.review_evidence(evidence_id, accepted=True, reviewed_at=NOW)

        proposal = build_profile_update_proposal(
            repository, preferences, profile_path=profile, created_at=NOW,
        )
        assert proposal["status"] == "pending_user_confirmation"
        update = proposal["belief_updates"][0]
        assert update["current_confidence"] == 0.8
        assert update["suggested_confidence"] == 0.765
        assert update["evidence"][0]["evidence_id"] == evidence_id
        assert proposal["prompt_updates"][0]["prompt_id"] == "question-a"
        assert profile.read_bytes() == before

        destination = write_profile_update_proposal(tmp_path / "proposals", proposal)
        written = yaml.safe_load(destination.read_text())
        assert written["policy"]["mutates_profile"] is False
    finally:
        connection.close()


def test_rejected_evidence_is_excluded_from_profile_proposal(tmp_path):
    connection, repository, preferences, profile, item_id = _setup(tmp_path)
    try:
        repository.record_feedback(item_id, kind="read", created_at=NOW)
        evidence_id = create_evidence(
            repository, preferences, item_id=item_id, belief_id="belief-a",
            prompt_id=None, relation="supports", claim_type="agent_inference",
            strength=1.0, source_quality="unknown", claim="Unverified inference",
            rationale="", profile_path=profile, created_at=NOW,
        )
        repository.review_evidence(evidence_id, accepted=False, reviewed_at=NOW)
        proposal = build_profile_update_proposal(
            repository, preferences, profile_path=profile, created_at=NOW,
        )
        assert proposal["belief_updates"] == []
    finally:
        connection.close()


def test_profile_proposal_reports_evidence_orphaned_by_profile_edit(tmp_path):
    connection, repository, preferences, profile, item_id = _setup(tmp_path)
    try:
        repository.record_feedback(item_id, kind="read", created_at=NOW)
        evidence_id = create_evidence(
            repository, preferences, item_id=item_id, belief_id="belief-a",
            prompt_id="question-a", relation="supports", claim_type="source_claim",
            strength=0.8, source_quality="primary", claim="Verified claim",
            rationale="", profile_path=profile, created_at=NOW,
        )
        repository.review_evidence(evidence_id, accepted=True, reviewed_at=NOW)

        document = yaml.safe_load(profile.read_text())
        document["long_term_agenda"][0].pop("current_beliefs")
        document["long_term_agenda"][0].pop("open_questions")
        profile.write_text(yaml.safe_dump(document, sort_keys=False))
        current = load_research_preferences(tmp_path / "topics.yaml", profile)

        proposal = build_profile_update_proposal(
            repository, current, profile_path=profile, created_at=NOW,
        )
        assert proposal["belief_updates"] == []
        assert proposal["prompt_updates"] == []
        assert {
            (row["target_kind"], row["target_id"])
            for row in proposal["orphaned_evidence"]
        } == {("belief", "belief-a"), ("prompt", "question-a")}
    finally:
        connection.close()
