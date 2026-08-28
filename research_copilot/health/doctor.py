"""Provenance report for Research Copilot code, config and library state."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DoctorReport:
    hermes_version: str
    git_commit: str
    schema_version: int | None
    database_path: str
    catalog_path: str
    catalog_hash: str
    topics_path: str
    topics_hash: str
    profile_path: str
    profile_hash: str
    enabled_sources: tuple[str, ...]
    last_source_run_at: str | None
    user_skill_override: str | None
    item_count: int
    recommendation_count: int
    feedback_count: int
    evidence_review_states: tuple[tuple[str, int], ...]
    duplicate_title_groups: int
    merge_evidence_count: int
    workflow_states: tuple[tuple[str, int], ...]
    agent_analysis_states: tuple[tuple[str, int], ...]
    user_learning_states: tuple[tuple[str, int], ...]
    delivery_outbox_states: tuple[tuple[str, int], ...]
    deep_research_delivery_states: tuple[tuple[str, int], ...]
    scout_delivery_states: tuple[tuple[str, int], ...]
    cooling_down_sources: tuple[str, ...]


def _sha256(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=project_root,
            check=True, capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def build_doctor_report(
    connection: sqlite3.Connection,
    *,
    database_path: str | Path,
    catalog_path: str | Path,
    topics_path: str | Path,
    profile_path: str | Path | None = None,
    project_root: str | Path,
    hermes_home: str | Path,
    hermes_version: str,
    git_commit: str | None = None,
) -> DoctorReport:
    schema = connection.execute(
        "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
    ).fetchone()
    # The YAML catalog is the behavioral source of truth.  The ``sources``
    # table records what a previous collection run observed and may be stale
    # after an operator disables a provider without running collection again.
    try:
        import yaml

        catalog_data = yaml.safe_load(Path(catalog_path).read_text(encoding="utf-8")) or {}
        rows = catalog_data.get("sources", [])
        enabled_sources = tuple(sorted(
            str(row["id"]) for row in rows
            if isinstance(row, dict) and row.get("enabled") is True and row.get("id")
        ))
    except (OSError, ValueError, yaml.YAMLError):
        enabled_sources = ()
    latest = connection.execute(
        "SELECT max(COALESCE(finished_at, started_at)) AS timestamp FROM source_runs"
    ).fetchone()
    override = Path(hermes_home) / "skills" / "research" / "paper" / "SKILL.md"
    tracked_skill = Path(project_root) / "skills" / "research" / "paper" / "SKILL.md"
    is_override = override.is_file()
    if is_override and tracked_skill.is_file():
        try:
            is_override = not override.samefile(tracked_skill)
        except OSError:
            pass
    catalog = Path(catalog_path)
    topics = Path(topics_path)
    if profile_path is not None:
        profile = Path(profile_path)
    else:
        profile = Path(hermes_home) / "research-copilot" / "research-config.yaml"
        legacy_profile = profile.with_name("research-profile.yaml")
        if not profile.exists() and legacy_profile.exists():
            profile = legacy_profile
    item_count = int(connection.execute("SELECT count(*) FROM research_items").fetchone()[0])
    recommendation_count = int(connection.execute("SELECT count(*) FROM recommendations").fetchone()[0])
    feedback_count = int(connection.execute("SELECT count(*) FROM feedback_events").fetchone()[0])
    evidence_review_states = tuple(
        (str(row[0]), int(row[1])) for row in connection.execute(
            "SELECT review_status, count(*) FROM evidence_records "
            "GROUP BY review_status ORDER BY review_status"
        )
    )
    duplicate_title_groups = int(connection.execute(
        """SELECT count(*) FROM (
               SELECT normalized_title FROM research_items
               WHERE normalized_title != '' GROUP BY normalized_title HAVING count(*) > 1
           )"""
    ).fetchone()[0])
    merge_evidence_count = int(connection.execute(
        "SELECT count(*) FROM item_merge_evidence"
    ).fetchone()[0])
    item_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(research_items)")
    }
    state_column = "workflow_state" if "workflow_state" in item_columns else "status"
    workflow_states = tuple(
        (str(row[0]), int(row[1])) for row in connection.execute(
            f"SELECT {state_column}, count(*) FROM research_items GROUP BY {state_column} ORDER BY {state_column}"
        )
    )
    agent_analysis_states = tuple(
        (str(row[0]), int(row[1])) for row in connection.execute(
            "SELECT agent_analysis_status, count(*) FROM research_items "
            "GROUP BY agent_analysis_status ORDER BY agent_analysis_status"
        )
    ) if "agent_analysis_status" in item_columns else ()
    user_learning_states = tuple(
        (str(row[0]), int(row[1])) for row in connection.execute(
            "SELECT user_learning_status, count(*) FROM research_items "
            "GROUP BY user_learning_status ORDER BY user_learning_status"
        )
    ) if "user_learning_status" in item_columns else ()
    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    delivery_outbox_states = tuple(
        (str(row[0]), int(row[1])) for row in connection.execute(
            "SELECT status, count(*) FROM recommendation_delivery_outbox "
            "GROUP BY status ORDER BY status"
        )
    ) if "recommendation_delivery_outbox" in tables else ()
    deep_research_delivery_states = tuple(
        (str(row[0]), int(row[1])) for row in connection.execute(
            "SELECT status, count(*) FROM deep_research_delivery_outbox "
            "GROUP BY status ORDER BY status"
        )
    ) if "deep_research_delivery_outbox" in tables else ()
    scout_delivery_states = tuple(
        (str(row[0]), int(row[1])) for row in connection.execute(
            "SELECT status, count(*) FROM scout_delivery_outbox "
            "GROUP BY status ORDER BY status"
        )
    ) if "scout_delivery_outbox" in tables else ()
    cooling_down_sources = tuple(
        str(row["source_id"]) for row in connection.execute(
            """
            SELECT source_id FROM source_runtime_state
            WHERE cooldown_until IS NOT NULL AND julianday(cooldown_until) > julianday('now')
            ORDER BY source_id
            """
        )
    )
    return DoctorReport(
        hermes_version=hermes_version,
        git_commit=git_commit or _git_commit(Path(project_root)),
        schema_version=int(schema["schema_version"]) if schema else None,
        database_path=str(Path(database_path)),
        catalog_path=str(catalog), catalog_hash=_sha256(catalog),
        topics_path=str(topics), topics_hash=_sha256(topics),
        profile_path=str(profile), profile_hash=_sha256(profile),
        enabled_sources=enabled_sources,
        last_source_run_at=latest["timestamp"] if latest else None,
        user_skill_override=str(override) if is_override else None,
        item_count=item_count,
        recommendation_count=recommendation_count,
        feedback_count=feedback_count,
        evidence_review_states=evidence_review_states,
        duplicate_title_groups=duplicate_title_groups,
        merge_evidence_count=merge_evidence_count,
        workflow_states=workflow_states,
        agent_analysis_states=agent_analysis_states,
        user_learning_states=user_learning_states,
        delivery_outbox_states=delivery_outbox_states,
        deep_research_delivery_states=deep_research_delivery_states,
        scout_delivery_states=scout_delivery_states,
        cooling_down_sources=cooling_down_sources,
    )


def render_doctor_report(report: DoctorReport) -> str:
    sources = ", ".join(report.enabled_sources) or "none"
    override = report.user_skill_override or "none"
    states = ", ".join(f"{name}={count}" for name, count in report.workflow_states) or "none"
    analysis_states = ", ".join(
        f"{name}={count}" for name, count in report.agent_analysis_states
    ) or "none"
    learning_states = ", ".join(
        f"{name}={count}" for name, count in report.user_learning_states
    ) or "none"
    outbox_states = ", ".join(
        f"{name}={count}" for name, count in report.delivery_outbox_states
    ) or "none"
    deep_delivery_states = ", ".join(
        f"{name}={count}" for name, count in report.deep_research_delivery_states
    ) or "none"
    scout_delivery_states = ", ".join(
        f"{name}={count}" for name, count in report.scout_delivery_states
    ) or "none"
    cooling = ", ".join(report.cooling_down_sources) or "none"
    evidence = ", ".join(
        f"{name}={count}" for name, count in report.evidence_review_states
    ) or "none"
    return "\n".join([
        "Research Copilot doctor",
        f"Hermes version: {report.hermes_version}",
        f"Git commit: {report.git_commit}",
        f"Library schema: {report.schema_version}",
        f"Database: {report.database_path}",
        f"Source Catalog: {report.catalog_path} ({report.catalog_hash})",
        f"Topics: {report.topics_path} ({report.topics_hash})",
        f"Research config: {report.profile_path} ({report.profile_hash})",
        f"Enabled sources: {sources}",
        f"Last source run: {report.last_source_run_at or 'never'}",
        f"Sources in cooldown: {cooling}",
        f"Library items: {report.item_count} ({states})",
        f"Agent analysis: {analysis_states}",
        f"User learning: {learning_states}",
        f"Recommendations: {report.recommendation_count}",
        f"Recommendation delivery outbox: {outbox_states}",
        f"Deep-research delivery outbox: {deep_delivery_states}",
        f"Scout delivery outbox: {scout_delivery_states}",
        f"Feedback events: {report.feedback_count}",
        f"Profile evidence: {evidence}",
        f"Duplicate normalized-title groups: {report.duplicate_title_groups}",
        f"Guarded identity merges: {report.merge_evidence_count}",
        f"User paper-skill override: {override}",
    ])
