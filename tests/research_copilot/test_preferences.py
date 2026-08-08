from __future__ import annotations

from datetime import datetime, timezone

import yaml
import pytest

from research_copilot.preferences import (
    ResearchConfigError,
    load_research_preferences,
    migrate_research_preferences,
)


def _write_pair(tmp_path, topics: dict, profile: dict):
    topics_path = tmp_path / "topics.yaml"
    profile_path = tmp_path / "research-profile.yaml"
    topics_path.write_text(yaml.safe_dump(topics, sort_keys=False), encoding="utf-8")
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    return topics_path, profile_path


def test_schema_v2_separates_discovery_terms_from_research_intent(tmp_path):
    paths = _write_pair(tmp_path, {
        "schema_version": 2,
        "topics": [{
            "id": "agent", "name": "Agents", "status": "active",
            "search_queries": ["agentic research"],
            "match_terms": ["research agent"], "exclude_terms": ["game agent"],
        }],
    }, {
        "schema_version": 2,
        "long_term_agenda": [{
            "id": "reliable-agents", "topic_ids": ["agent"], "priority": 0.9,
            "current_beliefs": [{
                "id": "recovery-matters", "statement": "Recovery is necessary",
                "confidence": 0.8, "updated_at": "2026-08-08",
            }],
            "open_questions": [{"id": "oq-1", "question": "How should recovery be evaluated?"}],
            "knowledge_gaps": ["Need real failure traces"],
        }],
    })
    preferences = load_research_preferences(*paths)
    assert preferences.active_topics[0].search_queries == ("agentic research",)
    assert preferences.active_topics[0].match_terms == ("research agent",)
    assert preferences.ranking_values("agent") == (
        0.9, ("How should recovery be evaluated?",),
    )
    assert preferences.agenda[0].open_question_refs[0].id == "oq-1"
    assert preferences.agenda[0].knowledge_gap_refs[0].text == "Need real failure traces"
    assert preferences.belief("recovery-matters").agenda_id == "reliable-agents"
    assert preferences.belief("recovery-matters").confidence == 0.8
    assert preferences.prompt("oq-1")[1].text == "How should recovery be evaluated?"


def test_schema_v2_rejects_unknown_topic_reference(tmp_path):
    paths = _write_pair(tmp_path, {
        "schema_version": 2, "topics": [],
    }, {
        "schema_version": 2,
        "long_term_agenda": [{"id": "x", "topic_ids": ["missing"]}],
    })
    with pytest.raises(ResearchConfigError, match="unknown topics"):
        load_research_preferences(*paths)


def test_v1_is_compatible_and_migration_is_preview_then_backed_up(tmp_path):
    paths = _write_pair(tmp_path, {
        "topics": [{
            "id": "agent", "name": "Agents", "status": "dormant",
            "priority": 0.8, "include": ["agentic research"],
            "exclude": ["game"], "open_questions": ["How to recover?"],
        }],
    }, {"long_term_agenda": []})
    compatible = load_research_preferences(*paths)
    assert compatible.topics[0].status == "inactive"
    assert compatible.ranking_values("agent") == (0.8, ("How to recover?",))

    preview = migrate_research_preferences(*paths)
    assert preview.changed is True
    assert preview.applied is False
    assert "schema_version" not in yaml.safe_load(paths[0].read_text())

    applied = migrate_research_preferences(
        *paths, apply=True,
        migrated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
    )
    assert applied.applied is True
    assert applied.topics_backup and applied.profile_backup
    migrated = load_research_preferences(*paths)
    assert migrated.topics_schema_version == 2
    assert migrated.profile_schema_version == 2
    assert migrated.topics[0].search_queries == ("Agents", "agentic research")
    assert migrated.agenda[0].topic_ids == ("agent",)


def test_duplicate_terms_are_rejected(tmp_path):
    paths = _write_pair(tmp_path, {
        "schema_version": 2,
        "topics": [{
            "id": "agent", "search_queries": ["agents", "agents"],
            "match_terms": [], "exclude_terms": [],
        }],
    }, {"schema_version": 2, "long_term_agenda": []})
    with pytest.raises(ResearchConfigError, match="duplicate values"):
        load_research_preferences(*paths)


def test_duplicate_belief_and_prompt_ids_are_rejected(tmp_path):
    topics = {
        "schema_version": 2,
        "topics": [{"id": "agent", "search_queries": [], "match_terms": [], "exclude_terms": []}],
    }
    duplicate_beliefs = _write_pair(tmp_path, topics, {
        "schema_version": 2,
        "long_term_agenda": [
            {"id": "a", "topic_ids": ["agent"], "current_beliefs": [{"id": "same", "statement": "A"}]},
            {"id": "b", "topic_ids": ["agent"], "current_beliefs": [{"id": "same", "statement": "B"}]},
        ],
    })
    with pytest.raises(ResearchConfigError, match="Duplicate belief id"):
        load_research_preferences(*duplicate_beliefs)

    duplicate_prompts = _write_pair(tmp_path, topics, {
        "schema_version": 2,
        "long_term_agenda": [
            {"id": "a", "topic_ids": ["agent"], "open_questions": [{"id": "same", "question": "A?"}]},
            {"id": "b", "topic_ids": ["agent"], "knowledge_gaps": [{"id": "same", "description": "B"}]},
        ],
    })
    with pytest.raises(ResearchConfigError, match="Duplicate profile prompt id"):
        load_research_preferences(*duplicate_prompts)


def test_migration_restores_both_files_when_second_atomic_write_fails(tmp_path, monkeypatch):
    paths = _write_pair(tmp_path, {
        "topics": [{"id": "agent", "include": ["research agent"]}],
    }, {"long_term_agenda": [{"id": "agent", "priority": 0.8}]})
    before = tuple(path.read_bytes() for path in paths)
    from utils import atomic_yaml_write as real_atomic_yaml_write
    import utils

    calls = 0

    def fail_second(path, value, *, sort_keys=False):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated profile write failure")
        return real_atomic_yaml_write(path, value, sort_keys=sort_keys)

    monkeypatch.setattr(utils, "atomic_yaml_write", fail_second)
    with pytest.raises(OSError, match="simulated"):
        migrate_research_preferences(
            *paths, apply=True,
            migrated_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        )
    assert tuple(path.read_bytes() for path in paths) == before
