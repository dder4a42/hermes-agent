"""Source-aware word-family, morphology, and etymology records."""

from __future__ import annotations

import hashlib
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .database import LearningDatabase


ANALYSIS_TYPES = frozenset({"modern_morphology", "historical_etymology"})
ANALYSIS_STATUSES = frozenset({"available", "not_found", "ambiguous", "opaque"})
SOURCE_LEVELS = frozenset({"authoritative", "deterministic", "llm_inferred"})
POS_CODES = {
    "1": "noun",
    "2": "verb",
    "3": "adjective",
    "4": "adverb",
    "5": "adjective",
}


def normalize_form(value: str) -> str:
    return " ".join(value.casefold().strip().replace("_", " ").split())


def sense_form(value: str) -> str:
    return normalize_form(value.partition("%")[0])


def sense_part_of_speech(value: str) -> str | None:
    _lemma, separator, remainder = value.partition("%")
    return POS_CODES.get(remainder[:1]) if separator else None


class LexicalAnalysisService:
    def __init__(self, database: LearningDatabase):
        self.database = database
        self.database.initialize()

    def import_oewn_derivations(
        self,
        path: str | Path,
        *,
        source_version: str = "2025",
        source_license: str = "CC-BY-4.0",
    ) -> dict:
        archive = Path(path).expanduser()
        if not archive.is_file():
            raise ValueError(f"Open English WordNet archive not found: {archive}")
        created = updated = skipped = 0
        errors: list[dict] = []
        now_text = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection, zipfile.ZipFile(archive) as source:
            current_senses = {
                row["source_sense_id"]: (row["lemma"], row["part_of_speech"])
                for row in connection.execute(
                    """
                    SELECT source_sense_id, lemma, part_of_speech
                    FROM word_senses
                    WHERE source_sense_id IS NOT NULL
                    """
                )
            }
            names = sorted(
                name
                for name in source.namelist()
                if name.startswith("entries-") and name.endswith(".json")
            )
            for name in names:
                try:
                    entries = json.loads(source.read(name))
                except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    errors.append({"file": name, "error": str(exc)})
                    continue
                for _lemma, parts_of_speech in entries.items():
                    for entry in parts_of_speech.values():
                        for sense in entry.get("sense", []):
                            source_sense = sense.get("id")
                            if source_sense not in current_senses:
                                continue
                            source_lemma, source_pos = current_senses[source_sense]
                            for target_sense in sense.get("derivation", []):
                                target_form = sense_form(target_sense)
                                if not target_form:
                                    skipped += 1
                                    continue
                                source_entry_id = f"{source_sense}->{target_sense}"
                                relation_id = str(
                                    uuid.uuid5(
                                        uuid.NAMESPACE_URL,
                                        f"oewn:{source_version}:{source_entry_id}",
                                    )
                                )
                                existed = connection.execute(
                                    """
                                    SELECT 1 FROM lexical_relations
                                    WHERE source = 'oewn' AND source_entry_id = ?
                                    """,
                                    (source_entry_id,),
                                ).fetchone()
                                connection.execute(
                                    """
                                    INSERT INTO lexical_relations(
                                        id, source_form, source_part_of_speech,
                                        source_sense_id, target_form,
                                        target_part_of_speech, target_sense_id,
                                        relation_type, affix, confidence, source_level,
                                        source, source_version, source_license,
                                        source_entry_id, created_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'derivation', NULL,
                                              1.0, 'authoritative', 'oewn', ?, ?, ?, ?)
                                    ON CONFLICT(source, source_entry_id) DO UPDATE SET
                                        source_form = excluded.source_form,
                                        source_part_of_speech = excluded.source_part_of_speech,
                                        source_sense_id = excluded.source_sense_id,
                                        target_form = excluded.target_form,
                                        target_part_of_speech = excluded.target_part_of_speech,
                                        target_sense_id = excluded.target_sense_id,
                                        source_version = excluded.source_version,
                                        source_license = excluded.source_license
                                    """,
                                    (
                                        relation_id,
                                        normalize_form(source_lemma),
                                        source_pos,
                                        source_sense,
                                        target_form,
                                        sense_part_of_speech(target_sense),
                                        target_sense,
                                        source_version,
                                        source_license,
                                        source_entry_id,
                                        now_text,
                                    ),
                                )
                                created += int(existed is None)
                                updated += int(existed is not None)
        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "errors": errors,
        }

    def upsert_analysis(self, payload: dict[str, Any]) -> dict:
        normalized = normalize_form(str(payload.get("form", "")))
        if not normalized:
            raise ValueError("analysis form is required")
        analysis_type = str(payload.get("analysis_type", ""))
        status = str(payload.get("status", "available"))
        source_level = str(payload.get("source_level", "llm_inferred"))
        if analysis_type not in ANALYSIS_TYPES:
            raise ValueError("unsupported analysis_type")
        if status not in ANALYSIS_STATUSES:
            raise ValueError("unsupported analysis status")
        if source_level not in SOURCE_LEVELS:
            raise ValueError("unsupported source_level")
        confidence = float(payload.get("confidence", 0.0))
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("analysis confidence must be between 0 and 1")
        content = payload.get("content")
        if not isinstance(content, dict):
            raise ValueError("analysis content must be a JSON object")
        model_name = payload.get("model_name")
        prompt_version = payload.get("prompt_version")
        if source_level == "llm_inferred" and (not model_name or not prompt_version):
            raise ValueError("LLM analysis requires model_name and prompt_version")
        source = str(payload.get("source") or source_level)
        source_version = str(payload.get("source_version") or model_name or "unknown")
        source_entry_id = str(payload.get("source_entry_id") or "")
        if not source_entry_id:
            identity = json.dumps(
                {
                    "form": normalized,
                    "part_of_speech": payload.get("part_of_speech"),
                    "source_sense_id": payload.get("source_sense_id"),
                    "analysis_type": analysis_type,
                    "source": source,
                    "source_version": source_version,
                    "prompt_version": prompt_version,
                },
                sort_keys=True,
            )
            source_entry_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        record_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{source_entry_id}"))
        now_text = datetime.now(timezone.utc).isoformat()
        with self.database.connect() as connection:
            existed = connection.execute(
                "SELECT 1 FROM lexical_analyses WHERE source = ? AND source_entry_id = ?",
                (source, source_entry_id),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO lexical_analyses(
                    id, normalized_form, part_of_speech, source_sense_id,
                    analysis_type, status, source_level, content_json,
                    explanation_zh, confidence, source, source_version,
                    source_license, source_entry_id, model_name, prompt_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_entry_id) DO UPDATE SET
                    normalized_form = excluded.normalized_form,
                    part_of_speech = excluded.part_of_speech,
                    source_sense_id = excluded.source_sense_id,
                    analysis_type = excluded.analysis_type,
                    status = excluded.status,
                    source_level = excluded.source_level,
                    content_json = excluded.content_json,
                    explanation_zh = excluded.explanation_zh,
                    confidence = excluded.confidence,
                    source_version = excluded.source_version,
                    source_license = excluded.source_license,
                    model_name = excluded.model_name,
                    prompt_version = excluded.prompt_version,
                    updated_at = excluded.updated_at
                """,
                (
                    record_id,
                    normalized,
                    payload.get("part_of_speech"),
                    payload.get("source_sense_id"),
                    analysis_type,
                    status,
                    source_level,
                    json.dumps(content, ensure_ascii=False, sort_keys=True),
                    payload.get("explanation_zh"),
                    confidence,
                    source,
                    source_version,
                    str(payload.get("source_license") or "generated content"),
                    source_entry_id,
                    model_name,
                    prompt_version,
                    now_text,
                    now_text,
                ),
            )
        return {"id": record_id, "created": existed is None, "source_entry_id": source_entry_id}

    def lookup(
        self,
        form: str,
        *,
        part_of_speech: str | None = None,
        source_sense_id: str | None = None,
    ) -> dict:
        key = (normalize_form(form), part_of_speech, source_sense_id)
        return self.lookup_many([key]).get(key, self._empty_result())

    def lookup_many(
        self, items: list[tuple[str, str | None, str | None]]
    ) -> dict[tuple[str, str | None, str | None], dict]:
        keys = list(
            dict.fromkeys(
                (normalize_form(form), part_of_speech, source_sense_id)
                for form, part_of_speech, source_sense_id in items
                if form.strip()
            )
        )
        result = {key: self._empty_result() for key in keys}
        if not keys:
            return result
        forms = list(dict.fromkeys(form for form, _pos, _sense in keys))
        placeholders = ", ".join("?" for _form in forms)
        with self.database.connect() as connection:
            relations = connection.execute(
                f"""
                SELECT * FROM lexical_relations
                WHERE source_form IN ({placeholders}) OR target_form IN ({placeholders})
                ORDER BY confidence DESC, source, source_entry_id
                """,
                (*forms, *forms),
            ).fetchall()
            analyses = connection.execute(
                f"""
                SELECT * FROM lexical_analyses
                WHERE normalized_form IN ({placeholders})
                ORDER BY
                    CASE source_level
                        WHEN 'authoritative' THEN 0
                        WHEN 'deterministic' THEN 1
                        ELSE 2
                    END,
                    confidence DESC, updated_at DESC
                """,
                forms,
            ).fetchall()
        for key in keys:
            form, part_of_speech, source_sense_id = key
            family = []
            seen_family = set()
            for row in relations:
                outgoing = row["source_form"] == form
                incoming = row["target_form"] == form
                if not outgoing and not incoming:
                    continue
                if outgoing and source_sense_id and row["source_sense_id"] not in {
                    None,
                    source_sense_id,
                }:
                    continue
                if incoming and source_sense_id and row["target_sense_id"] not in {
                    None,
                    source_sense_id,
                }:
                    continue
                current_pos = (
                    row["source_part_of_speech"]
                    if outgoing
                    else row["target_part_of_speech"]
                )
                if part_of_speech and current_pos not in {None, part_of_speech}:
                    continue
                related_form = row["target_form"] if outgoing else row["source_form"]
                related_pos = (
                    row["target_part_of_speech"]
                    if outgoing
                    else row["source_part_of_speech"]
                )
                identity = (related_form, related_pos, row["relation_type"])
                if identity in seen_family or related_form == form:
                    continue
                seen_family.add(identity)
                family.append(
                    {
                        "form": related_form,
                        "part_of_speech": related_pos,
                        "relation_type": row["relation_type"],
                        "direction": "outgoing" if outgoing else "incoming",
                        "affix": row["affix"],
                        "confidence": row["confidence"],
                        "source_level": row["source_level"],
                        "source": row["source"],
                    }
                )
            matching_analyses = []
            for row in analyses:
                if row["normalized_form"] != form:
                    continue
                if row["part_of_speech"] not in {None, part_of_speech}:
                    continue
                if row["source_sense_id"] not in {None, source_sense_id}:
                    continue
                matching_analyses.append(self._analysis_dict(row))
            resolved_types = {
                item["analysis_type"]
                for item in matching_analyses
                if item["status"] == "available"
                or item["source_level"] == "llm_inferred"
            }
            result[key] = {
                "word_family": family[:24],
                "analyses": matching_analyses,
                "needs_inference": {
                    analysis_type: analysis_type not in resolved_types
                    for analysis_type in sorted(ANALYSIS_TYPES)
                },
            }
        return result

    @staticmethod
    def _analysis_dict(row) -> dict:
        return {
            "analysis_type": row["analysis_type"],
            "status": row["status"],
            "source_level": row["source_level"],
            "content": json.loads(row["content_json"]),
            "explanation_zh": row["explanation_zh"],
            "confidence": row["confidence"],
            "source": row["source"],
            "source_version": row["source_version"],
            "model_name": row["model_name"],
            "prompt_version": row["prompt_version"],
        }

    @staticmethod
    def _empty_result() -> dict:
        return {
            "word_family": [],
            "analyses": [],
            "needs_inference": {
                "historical_etymology": True,
                "modern_morphology": True,
            },
        }
