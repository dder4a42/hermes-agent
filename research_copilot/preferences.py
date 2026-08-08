"""Validated Topic/Profile configuration with schema-v1 compatibility."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any

import yaml


class ResearchConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TopicConfig:
    id: str
    name: str
    status: str
    description: str
    search_queries: tuple[str, ...]
    match_terms: tuple[str, ...]
    exclude_terms: tuple[str, ...]
    legacy_priority: float | None = None
    legacy_open_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgendaConfig:
    id: str
    name: str
    topic_ids: tuple[str, ...]
    priority: float
    core_question: str
    open_questions: tuple[str, ...]
    knowledge_gaps: tuple[str, ...]
    open_question_refs: tuple["ProfilePrompt", ...] = ()
    knowledge_gap_refs: tuple["ProfilePrompt", ...] = ()


@dataclass(frozen=True)
class ProfilePrompt:
    id: str
    text: str


@dataclass(frozen=True)
class BeliefConfig:
    id: str
    agenda_id: str
    statement: str
    confidence: float
    updated_at: str


@dataclass(frozen=True)
class ResearchPreferences:
    topics_schema_version: int
    profile_schema_version: int
    topics: tuple[TopicConfig, ...]
    agenda: tuple[AgendaConfig, ...]
    beliefs: tuple[BeliefConfig, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def active_topics(self) -> tuple[TopicConfig, ...]:
        return tuple(topic for topic in self.topics if topic.status == "active")

    def ranking_values(self, topic_id: str) -> tuple[float, tuple[str, ...]]:
        linked = [entry for entry in self.agenda if topic_id in entry.topic_ids]
        if linked:
            priority = max(entry.priority for entry in linked)
            questions = tuple(dict.fromkeys(
                question for entry in linked for question in entry.open_questions
            ))
            return priority, questions
        topic = next((value for value in self.topics if value.id == topic_id), None)
        if topic is not None and topic.legacy_priority is not None:
            return topic.legacy_priority, topic.legacy_open_questions
        return 0.5, ()

    def belief(self, belief_id: str) -> BeliefConfig:
        match = next((value for value in self.beliefs if value.id == belief_id), None)
        if match is None:
            raise ResearchConfigError(f"Unknown belief id: {belief_id}")
        return match

    def prompt(self, prompt_id: str) -> tuple[str, ProfilePrompt]:
        for agenda in self.agenda:
            for prompt in (*agenda.open_question_refs, *agenda.knowledge_gap_refs):
                if prompt.id == prompt_id:
                    return agenda.id, prompt
        raise ResearchConfigError(f"Unknown question/gap id: {prompt_id}")


@dataclass(frozen=True)
class PreferenceMigrationReport:
    changed: bool
    applied: bool
    topics_backup: str | None
    profile_backup: str | None
    warnings: tuple[str, ...]


def _load(path: Path, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ResearchConfigError(f"Cannot read {label} {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ResearchConfigError(f"Invalid {label} YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise ResearchConfigError(f"{label} must be a mapping: {path}")
    return value


def _strings(value: Any, location: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ResearchConfigError(f"{location} must be a list of strings")
    cleaned = tuple(item.strip() for item in value if item.strip())
    if len(cleaned) != len(set(cleaned)):
        raise ResearchConfigError(f"{location} contains duplicate values")
    return cleaned


def _question_strings(value: Any, location: str) -> tuple[str, ...]:
    return tuple(prompt.text for prompt in _profile_prompts(value, location))


def _profile_prompts(value: Any, location: str) -> tuple[ProfilePrompt, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ResearchConfigError(f"{location} must be a list")
    prompts: list[ProfilePrompt] = []
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    scope, _separator, field = location.partition(".")
    scope_id = scope.removeprefix("agenda ").removeprefix("topic ").strip()
    field_id = field.replace("_", "-").removesuffix("s") or "prompt"
    for index, item in enumerate(value):
        if isinstance(item, str):
            text = item.strip()
            prompt_id = f"{scope_id}:{field_id}-{index + 1}"
        elif isinstance(item, dict):
            text = str(item.get("question") or item.get("description") or "").strip()
            prompt_id = str(item.get("id") or f"{scope_id}:{field_id}-{index + 1}").strip()
        else:
            raise ResearchConfigError(f"{location}[{index}] must be a string or mapping")
        if not text:
            raise ResearchConfigError(f"{location}[{index}] is empty")
        if not prompt_id:
            raise ResearchConfigError(f"{location}[{index}].id is empty")
        if prompt_id in seen_ids:
            raise ResearchConfigError(f"{location} contains duplicate id: {prompt_id}")
        seen_ids.add(prompt_id)
        if text in seen_text:
            continue
        seen_text.add(text)
        prompts.append(ProfilePrompt(id=prompt_id, text=text))
    return tuple(prompts)


def load_research_preferences(
    topics_path: str | Path,
    profile_path: str | Path,
) -> ResearchPreferences:
    topics_doc = _load(Path(topics_path), "topics configuration")
    profile_doc = _load(Path(profile_path), "research profile")
    topics_version = int(topics_doc.get("schema_version", 1))
    profile_version = int(profile_doc.get("schema_version", 1))
    if topics_version not in {1, 2}:
        raise ResearchConfigError(f"Unsupported topics schema {topics_version}; expected 1 or 2")
    if profile_version not in {1, 2}:
        raise ResearchConfigError(f"Unsupported research profile schema {profile_version}; expected 1 or 2")

    topic_rows = topics_doc.get("topics")
    if not isinstance(topic_rows, list):
        raise ResearchConfigError("topics must be a list")
    warnings: list[str] = []
    topics: list[TopicConfig] = []
    seen_topics: set[str] = set()
    for index, raw in enumerate(topic_rows):
        if not isinstance(raw, dict):
            raise ResearchConfigError(f"topics[{index}] must be a mapping")
        topic_id = str(raw.get("id") or "").strip()
        if not topic_id:
            raise ResearchConfigError(f"topics[{index}].id is required")
        if topic_id in seen_topics:
            raise ResearchConfigError(f"Duplicate topic id: {topic_id}")
        seen_topics.add(topic_id)
        status = str(raw.get("status") or "active").strip().lower()
        if status == "dormant" and topics_version == 1:
            status = "inactive"
            warnings.append(f"topic {topic_id}: status 'dormant' is deprecated; use 'inactive'")
        if status not in {"active", "inactive"}:
            raise ResearchConfigError(f"topic {topic_id}: status must be active or inactive")
        include = _strings(raw.get("include"), f"topic {topic_id}.include")
        exclude = _strings(raw.get("exclude"), f"topic {topic_id}.exclude")
        search_queries = _strings(raw.get("search_queries"), f"topic {topic_id}.search_queries")
        match_terms = _strings(raw.get("match_terms"), f"topic {topic_id}.match_terms")
        exclude_terms = _strings(raw.get("exclude_terms"), f"topic {topic_id}.exclude_terms")
        name = str(raw.get("name") or topic_id).strip()
        if topics_version == 1:
            search_queries = search_queries or tuple(dict.fromkeys((name, *include)))
            match_terms = match_terms or tuple(dict.fromkeys((name, *include)))
            exclude_terms = exclude_terms or exclude
        elif any(key in raw for key in ("include", "exclude", "priority", "open_questions")):
            warnings.append(
                f"topic {topic_id}: legacy include/exclude/priority/open_questions fields are ignored by schema v2"
            )
        legacy_priority = None
        if topics_version == 1:
            legacy_priority = float(raw.get("priority", 0.5))
            if not 0 <= legacy_priority <= 1:
                raise ResearchConfigError(f"topic {topic_id}.priority must be between 0 and 1")
        topics.append(TopicConfig(
            id=topic_id,
            name=name,
            status=status,
            description=str(raw.get("description") or "").strip(),
            search_queries=search_queries or (name,),
            match_terms=match_terms,
            exclude_terms=exclude_terms,
            legacy_priority=legacy_priority,
            legacy_open_questions=_question_strings(
                raw.get("open_questions"), f"topic {topic_id}.open_questions"
            ) if topics_version == 1 else (),
        ))

    agenda_rows = profile_doc.get("long_term_agenda", [])
    if not isinstance(agenda_rows, list):
        raise ResearchConfigError("long_term_agenda must be a list")
    agenda: list[AgendaConfig] = []
    beliefs: list[BeliefConfig] = []
    seen_agenda: set[str] = set()
    seen_beliefs: set[str] = set()
    seen_prompts: set[str] = set()
    for index, raw in enumerate(agenda_rows):
        if not isinstance(raw, dict):
            raise ResearchConfigError(f"long_term_agenda[{index}] must be a mapping")
        agenda_id = str(raw.get("id") or "").strip()
        if not agenda_id:
            raise ResearchConfigError(f"long_term_agenda[{index}].id is required")
        if agenda_id in seen_agenda:
            raise ResearchConfigError(f"Duplicate agenda id: {agenda_id}")
        seen_agenda.add(agenda_id)
        topic_ids = _strings(raw.get("topic_ids"), f"agenda {agenda_id}.topic_ids")
        if not topic_ids and profile_version == 1 and agenda_id in seen_topics:
            topic_ids = (agenda_id,)
            warnings.append(f"agenda {agenda_id}: inferred legacy topic_ids: [{agenda_id}]")
        unknown = sorted(set(topic_ids) - seen_topics)
        if unknown:
            raise ResearchConfigError(
                f"agenda {agenda_id} references unknown topics: {', '.join(unknown)}"
            )
        if profile_version == 2 and not topic_ids:
            raise ResearchConfigError(f"agenda {agenda_id}.topic_ids must not be empty")
        priority = float(raw.get("priority", 0.5))
        if not 0 <= priority <= 1:
            raise ResearchConfigError(f"agenda {agenda_id}.priority must be between 0 and 1")
        open_question_refs = _profile_prompts(
            raw.get("open_questions"), f"agenda {agenda_id}.open_questions",
        )
        knowledge_gap_refs = _profile_prompts(
            raw.get("knowledge_gaps"), f"agenda {agenda_id}.knowledge_gaps",
        )
        for prompt in (*open_question_refs, *knowledge_gap_refs):
            if prompt.id in seen_prompts:
                raise ResearchConfigError(f"Duplicate profile prompt id: {prompt.id}")
            seen_prompts.add(prompt.id)
        belief_rows = raw.get("current_beliefs", [])
        if not isinstance(belief_rows, list):
            raise ResearchConfigError(f"agenda {agenda_id}.current_beliefs must be a list")
        for belief_index, belief in enumerate(belief_rows):
            if not isinstance(belief, dict):
                raise ResearchConfigError(
                    f"agenda {agenda_id}.current_beliefs[{belief_index}] must be a mapping"
                )
            belief_id = str(belief.get("id") or "").strip()
            statement = str(belief.get("statement") or "").strip()
            if not belief_id or not statement:
                raise ResearchConfigError(
                    f"agenda {agenda_id}.current_beliefs[{belief_index}] requires id and statement"
                )
            if belief_id in seen_beliefs:
                raise ResearchConfigError(f"Duplicate belief id: {belief_id}")
            confidence = float(belief.get("confidence", 0.5))
            if not 0 <= confidence <= 1:
                raise ResearchConfigError(
                    f"belief {belief_id}.confidence must be between 0 and 1"
                )
            seen_beliefs.add(belief_id)
            beliefs.append(BeliefConfig(
                id=belief_id, agenda_id=agenda_id, statement=statement,
                confidence=confidence,
                updated_at=str(belief.get("updated_at") or "").strip(),
            ))
        agenda.append(AgendaConfig(
            id=agenda_id,
            name=str(raw.get("name") or agenda_id).strip(),
            topic_ids=topic_ids,
            priority=priority,
            core_question=str(raw.get("core_question") or "").strip(),
            open_questions=tuple(prompt.text for prompt in open_question_refs),
            knowledge_gaps=tuple(prompt.text for prompt in knowledge_gap_refs),
            open_question_refs=open_question_refs,
            knowledge_gap_refs=knowledge_gap_refs,
        ))
    return ResearchPreferences(
        topics_schema_version=topics_version,
        profile_schema_version=profile_version,
        topics=tuple(topics),
        agenda=tuple(agenda),
        beliefs=tuple(beliefs),
        warnings=tuple(warnings),
    )


def migrate_research_preferences(
    topics_path: str | Path,
    profile_path: str | Path,
    *,
    apply: bool = False,
    migrated_at: datetime | None = None,
) -> PreferenceMigrationReport:
    """Build/apply schema-v2 Topic/Profile documents with timestamped backups."""
    topics_path = Path(topics_path)
    profile_path = Path(profile_path)
    topics_doc = _load(topics_path, "topics configuration")
    profile_doc = _load(profile_path, "research profile")
    if int(topics_doc.get("schema_version", 1)) not in {1, 2}:
        raise ResearchConfigError("Only topics schema 1 or 2 can be migrated")
    if int(profile_doc.get("schema_version", 1)) not in {1, 2}:
        raise ResearchConfigError("Only research profile schema 1 or 2 can be migrated")

    topic_rows = topics_doc.get("topics")
    agenda_rows = profile_doc.get("long_term_agenda", [])
    if not isinstance(topic_rows, list) or not isinstance(agenda_rows, list):
        raise ResearchConfigError("topics and long_term_agenda must be lists")
    agenda_by_id = {
        str(row.get("id")): row for row in agenda_rows
        if isinstance(row, dict) and row.get("id")
    }
    warnings: list[str] = []
    for raw in topic_rows:
        if not isinstance(raw, dict):
            raise ResearchConfigError("Every topic must be a mapping")
        topic_id = str(raw.get("id") or "").strip()
        name = str(raw.get("name") or topic_id).strip()
        include = raw.pop("include", [])
        exclude = raw.pop("exclude", [])
        priority = raw.pop("priority", None)
        open_questions = raw.pop("open_questions", None)
        if "search_queries" not in raw:
            raw["search_queries"] = list(dict.fromkeys([name, *include]))
        if "match_terms" not in raw:
            raw["match_terms"] = list(dict.fromkeys([name, *include]))
        if "exclude_terms" not in raw:
            raw["exclude_terms"] = list(exclude)
        if raw.get("status") == "dormant":
            raw["status"] = "inactive"
        agenda = agenda_by_id.get(topic_id)
        if agenda is None and (priority is not None or open_questions):
            agenda = {
                "id": topic_id,
                "name": name,
                "topic_ids": [topic_id],
                "priority": float(priority if priority is not None else 0.5),
                "open_questions": list(open_questions or []),
                "knowledge_gaps": [],
            }
            agenda_rows.append(agenda)
            agenda_by_id[topic_id] = agenda
        elif agenda is not None:
            agenda.setdefault("topic_ids", [topic_id])
            if priority is not None and "priority" in agenda and float(priority) != float(agenda["priority"]):
                warnings.append(f"agenda {topic_id}: kept profile priority over legacy topic priority")
            elif priority is not None:
                agenda["priority"] = float(priority)
            if open_questions and agenda.get("open_questions") and agenda["open_questions"] != open_questions:
                warnings.append(f"agenda {topic_id}: kept profile open_questions over legacy topic questions")
            elif open_questions:
                agenda["open_questions"] = list(open_questions)
    topics_doc["schema_version"] = 2
    profile_doc["schema_version"] = 2
    changed = (
        topics_doc != _load(topics_path, "topics configuration")
        or profile_doc != _load(profile_path, "research profile")
    )
    # Validate the generated documents before any user state is touched.
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        temp_topics = temporary / "topics.yaml"
        temp_profile = temporary / "research-profile.yaml"
        temp_topics.write_text(yaml.safe_dump(topics_doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        temp_profile.write_text(yaml.safe_dump(profile_doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
        load_research_preferences(temp_topics, temp_profile)
    if not apply or not changed:
        return PreferenceMigrationReport(changed, False, None, None, tuple(warnings))

    now = migrated_at or datetime.now(timezone.utc)
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = topics_path.parent / "backups" / f"preferences-v1-{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    topics_backup = backup_dir / topics_path.name
    profile_backup = backup_dir / profile_path.name
    shutil.copy2(topics_path, topics_backup)
    shutil.copy2(profile_path, profile_backup)
    topics_doc["updated_at"] = now.astimezone(timezone.utc).isoformat()
    profile_doc["updated_at"] = now.astimezone(timezone.utc).isoformat()
    from utils import atomic_yaml_write

    try:
        atomic_yaml_write(topics_path, topics_doc, sort_keys=False)
        atomic_yaml_write(profile_path, profile_doc, sort_keys=False)
        load_research_preferences(topics_path, profile_path)
    except Exception:
        shutil.copy2(topics_backup, topics_path)
        shutil.copy2(profile_backup, profile_path)
        raise
    return PreferenceMigrationReport(
        True, True, str(topics_backup), str(profile_backup), tuple(warnings)
    )
