"""One-way, selective legacy JSONL to Research Library migration."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from research_copilot.library import (
    LibraryRepository, ResearchItemDraft, SourceEvidence, TopicMatch,
    connect_library, initialize_library,
)


def _jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            rows.append({"_invalid_line": line_number})
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


@dataclass(frozen=True)
class MigrationPlan:
    data_dir: str
    candidate_count: int
    recommendation_count: int
    interaction_count: int
    selected_candidate_ids: tuple[str, ...]
    archive_candidate_count: int
    missing_candidate_ids: tuple[str, ...]
    duplicate_candidate_ids: tuple[str, ...]
    invalid_candidate_lines: tuple[int, ...]
    invalid_recommendation_lines: tuple[int, ...]
    invalid_interaction_lines: tuple[int, ...]

    @property
    def can_apply(self) -> bool:
        return not self.missing_candidate_ids and not self.duplicate_candidate_ids


def build_migration_plan(data_dir: str | Path) -> MigrationPlan:
    directory = Path(data_dir)
    candidates = _jsonl(directory / "candidates.jsonl")
    recommendations = _jsonl(directory / "recommendations.jsonl")
    interactions = _jsonl(directory / "interactions.jsonl")
    invalid_candidates = tuple(int(row["_invalid_line"]) for row in candidates if "_invalid_line" in row)
    invalid_recommendations = tuple(int(row["_invalid_line"]) for row in recommendations if "_invalid_line" in row)
    invalid_interactions = tuple(int(row["_invalid_line"]) for row in interactions if "_invalid_line" in row)
    valid_candidates = [row for row in candidates if "_invalid_line" not in row]
    valid_recommendations = [row for row in recommendations if "_invalid_line" not in row]
    valid_interactions = [row for row in interactions if "_invalid_line" not in row]

    counts: dict[str, int] = {}
    for candidate in valid_candidates:
        item_id = str(candidate.get("id") or "").strip()
        if item_id:
            counts[item_id] = counts.get(item_id, 0) + 1
    duplicate_ids = tuple(sorted(item_id for item_id, count in counts.items() if count > 1))
    referenced = {
        str(row.get("id") or row.get("item_id") or "").strip()
        for row in valid_recommendations
    }
    referenced.update(
        str(row.get("item_id") or "").strip()
        for row in valid_interactions
    )
    referenced.discard("")
    available = set(counts)
    selected = tuple(sorted(referenced & available))
    missing = tuple(sorted(referenced - available))
    return MigrationPlan(
        data_dir=str(directory), candidate_count=len(valid_candidates),
        recommendation_count=len(valid_recommendations),
        interaction_count=len(valid_interactions),
        selected_candidate_ids=selected,
        archive_candidate_count=max(0, len(valid_candidates) - len(selected)),
        missing_candidate_ids=missing,
        duplicate_candidate_ids=duplicate_ids,
        invalid_candidate_lines=invalid_candidates,
        invalid_recommendation_lines=invalid_recommendations,
        invalid_interaction_lines=invalid_interactions,
    )


def render_migration_plan(plan: MigrationPlan) -> str:
    lines = [
        "Research Library migration dry-run",
        f"Legacy data: {plan.data_dir}",
        f"Candidates: {plan.candidate_count}",
        f"Recommendations: {plan.recommendation_count}",
        f"Interactions: {plan.interaction_count}",
        f"Candidates selected for import: {len(plan.selected_candidate_ids)}",
        f"Candidates to archive only: {plan.archive_candidate_count}",
        f"Missing referenced candidates: {len(plan.missing_candidate_ids)}",
        f"Duplicate candidate ids: {len(plan.duplicate_candidate_ids)}",
        f"Invalid JSONL lines: candidates={len(plan.invalid_candidate_lines)}, "
        f"recommendations={len(plan.invalid_recommendation_lines)}, "
        f"interactions={len(plan.invalid_interaction_lines)}",
        f"Apply readiness: {'ready' if plan.can_apply else 'blocked'}",
    ]
    if plan.missing_candidate_ids:
        lines.append("Missing ids: " + ", ".join(plan.missing_candidate_ids))
    if plan.duplicate_candidate_ids:
        lines.append("Duplicate ids: " + ", ".join(plan.duplicate_candidate_ids))
    return "\n".join(lines)


@dataclass(frozen=True)
class MigrationResult:
    backup_dir: str
    database: str
    imported_items: int
    imported_recommendations: int
    imported_interactions: int
    mapping_report: str


def _load_json(path: Path, default: dict) -> dict:
    if not path.is_file():
        return default
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else default


def _timestamp(value: object, fallback: datetime) -> datetime:
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _backup(data_dir: Path, backup_root: Path, extra_paths: tuple[Path, ...]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_root / f"{stamp}-pre-library"
    destination.mkdir(parents=True, exist_ok=False)
    copied: list[Path] = []
    for source in (*sorted(data_dir.glob("*")), *extra_paths):
        if not source.exists() or source.name in {"library.db", "library.db-wal", "library.db-shm"}:
            continue
        target = destination / ("legacy-data" if source.parent == data_dir else "profile") / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target)
            copied.extend(path for path in target.rglob("*") if path.is_file())
        elif source.is_file():
            shutil.copy2(source, target)
            copied.append(target)
    manifest = destination / "SHA256SUMS"
    lines = [f"{_sha256(path)}  {path.relative_to(destination)}" for path in sorted(copied)]
    manifest.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    for line in lines:
        expected, relative = line.split("  ", 1)
        if _sha256(destination / relative) != expected:
            raise RuntimeError(f"Backup verification failed: {relative}")
    return destination


def _catalog_rows(registry: dict, topic_ids: set[str]) -> list[dict]:
    rows = [
        {"id": "huggingface-daily", "provider": "huggingface_daily", "display_name": "Hugging Face Daily Papers", "type": "paper_index", "enabled": True, "tier": .9, "topics": ["*"], "budget": {"max_requests": 8, "max_items": 100}, "options": {}},
        {"id": "semantic-scholar", "provider": "semantic_scholar", "display_name": "Semantic Scholar", "type": "paper_index", "enabled": True, "tier": .9, "topics": ["*"], "budget": {"max_requests": 30, "max_items": 100}, "options": {}},
        {"id": "github-trending", "provider": "github_trending", "display_name": "GitHub Trending", "type": "code", "enabled": True, "tier": .75, "topics": ["*"], "budget": {"max_requests": 8, "max_items": 50}, "options": {}},
        {"id": "alphaxiv", "provider": "alphaxiv", "display_name": "AlphaXiv", "type": "paper_discussion", "enabled": True, "tier": .8, "topics": ["*"], "budget": {"max_requests": 8, "max_items": 50}, "options": {}},
        {"id": "arxiv", "provider": "arxiv", "display_name": "arXiv", "type": "paper_index", "enabled": False, "tier": .7, "topics": ["*"], "budget": {"max_requests": 12, "max_items": 100}, "options": {}},
        {"id": "gmail-newsletters", "provider": "gmail_newsletter", "display_name": "Gmail Newsletters", "type": "newsletter", "enabled": False, "tier": .7, "topics": ["*"], "budget": {"max_requests": 35, "max_items": 30}, "options": {"lookback_days": 7, "max_messages": 30}},
    ]
    domains: set[str] = set()
    known = {row["id"] for row in rows}
    for old in registry.get("sources", []):
        if not isinstance(old, dict):
            continue
        domains.update(str(x) for x in old.get("domains", []) if str(x).strip())
        feed = str(old.get("feed_url") or "").strip()
        source_id = str(old.get("id") or "").strip()
        if feed and source_id and source_id not in known:
            topics = [str(value) for value in old.get("topics", []) if str(value) in topic_ids]
            rows.append({"id": source_id, "provider": "rss", "display_name": str(old.get("name") or source_id), "type": str(old.get("type") or "feed"), "enabled": True, "tier": max(0.0, min(1.0, float(old.get("tier", .5)))), "topics": topics or ["*"], "budget": {"max_requests": 2, "max_items": 30}, "options": {"feed_url": feed}})
            known.add(source_id)
    rows.append({"id": "tavily", "provider": "tavily", "display_name": "Tavily Search", "type": "web_search", "enabled": True, "tier": .65, "topics": ["*"], "budget": {"max_requests": 30, "max_items": 100}, "options": {"domains": sorted(domains), "search_depth": "advanced"}})
    return rows


def apply_migration(
    data_dir: str | Path, *, profile_home: str | Path | None = None,
) -> MigrationResult:
    """Back up legacy state and atomically create the selective Library database."""
    directory = Path(data_dir)
    home = Path(profile_home) if profile_home is not None else directory.parent
    plan = build_migration_plan(directory)
    if not plan.can_apply:
        raise RuntimeError("Migration audit is blocked; fix missing or duplicate ids first")
    database = directory / "library.db"
    if database.exists():
        raise FileExistsError(f"Refusing to overwrite existing Research Library: {database}")
    now = datetime.now(timezone.utc)
    backup = _backup(
        directory, home / "research-copilot-backups",
        tuple(path for path in (home / "cron" / "jobs.json", home / "skills" / "research" / "paper" / "SKILL.md") if path.exists()),
    )
    topics_doc = _load_json(directory / "topics.json", {"topics": []})
    profile_doc = _load_json(directory / "research_profile.json", {"schema_version": 1})
    registry = _load_json(directory / "source_registry.json", {"sources": []})
    topic_ids = {str(row.get("id")) for row in topics_doc.get("topics", []) if isinstance(row, dict) and row.get("id")}
    catalog = {"schema_version": 1, "sources": _catalog_rows(registry, topic_ids)}
    for path, value in ((directory / "topics.yaml", topics_doc), (directory / "research-profile.yaml", profile_doc), (directory / "sources.yaml", catalog)):
        path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")

    selected = set(plan.selected_candidate_ids)
    candidates = {str(row.get("id")): row for row in _jsonl(directory / "candidates.jsonl") if "_invalid_line" not in row and str(row.get("id")) in selected}
    topic_name_to_id = {str(row.get("name")): str(row.get("id")) for row in topics_doc.get("topics", []) if isinstance(row, dict)}
    catalog_ids = {row["id"] for row in catalog["sources"]}
    source_aliases = {"hf_daily": "huggingface-daily", "semantic_scholar": "semantic-scholar", "github_trending": "github-trending", "arxiv_api_fallback": "arxiv", "source_registry_search": "tavily", "newsletter": "gmail-newsletters"}
    temporary = directory / f"library.db.migrating-{os.getpid()}"
    mapping: dict[str, str] = {}
    rec_count = feedback_count = 0
    connection = connect_library(temporary)
    try:
        initialize_library(connection, migrated_at=now.isoformat())
        repo = LibraryRepository(connection)
        for source in catalog["sources"]:
            repo.upsert_source(source_id=source["id"], provider=source["provider"], display_name=source["display_name"], source_type=source["type"], tier=float(source["tier"]), enabled=bool(source["enabled"]), config=source.get("options", {}), now=now)
        for old_id, row in candidates.items():
            evidence = next((x for x in row.get("sources", []) if isinstance(x, dict)), {})
            raw_source = str(evidence.get("name") or "arxiv")
            source_id = source_aliases.get(raw_source, raw_source)
            if source_id not in catalog_ids:
                source_id = "tavily"
            matches = []
            for match in row.get("topics", []):
                if not isinstance(match, dict):
                    continue
                topic_id = topic_name_to_id.get(str(match.get("name")), str(match.get("id") or ""))
                if topic_id:
                    matches.append(TopicMatch(topic_id=topic_id, confidence=max(0.0, min(1.0, float(match.get("confidence", .5))))))
            result = repo.upsert_item(
                ResearchItemDraft(title=str(row.get("title") or old_id), item_type=str(row.get("type") or "research_signal"), summary=str(row.get("summary") or ""), url=str(row.get("url") or ""), authors=tuple(str(x) for x in row.get("authors", [])), published_at=str(row.get("published") or "") or None, arxiv_id=str(row.get("arxiv_id") or ""), metadata={"legacy_id": old_id, "legacy_status": row.get("status")}),
                source=SourceEvidence(source_id=source_id, metadata={"legacy_source": raw_source}), topics=tuple(matches), discovered_at=_timestamp(row.get("discovered_at"), now),
            )
            mapping[old_id] = result.item_id
        for row in _jsonl(directory / "recommendations.jsonl"):
            old_id = str(row.get("id") or row.get("item_id") or "")
            if old_id not in mapping:
                continue
            score = max(0.0, min(1.0, float(row.get("score", row.get("final_score", 0)))))
            repo.record_recommendation(mapping[old_id], score=score, score_breakdown=dict(candidates[old_id].get("score_dimensions") or {}), recommended_at=_timestamp(row.get("recommended_at"), now), rationale=str(row.get("brief") or row.get("rationale") or ""))
            rec_count += 1
        for row in _jsonl(directory / "interactions.jsonl"):
            old_id = str(row.get("item_id") or "")
            if old_id not in mapping:
                continue
            kind = str(row.get("kind") or row.get("type") or "note")
            if kind not in {"save", "read", "skip", "archive", "note"}:
                kind = "note"
            repo.record_feedback(mapping[old_id], kind=kind, created_at=_timestamp(row.get("created_at") or row.get("at"), now), payload={"legacy": row})
            feedback_count += 1
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    connection.close()
    os.replace(temporary, database)
    report_path = backup / "migration-report.json"
    report_path.write_text(json.dumps({"migrated_at": now.isoformat(), "legacy_candidates": plan.candidate_count, "archived_in_backup_only": plan.archive_candidate_count, "item_id_mapping": mapping, "recommendations": rec_count, "interactions": feedback_count}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return MigrationResult(str(backup), str(database), len(mapping), rec_count, feedback_count, str(report_path))
