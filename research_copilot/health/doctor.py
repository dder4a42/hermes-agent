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
    enabled_sources: tuple[str, ...]
    last_source_run_at: str | None
    user_skill_override: str | None


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
    project_root: str | Path,
    hermes_home: str | Path,
    hermes_version: str,
    git_commit: str | None = None,
) -> DoctorReport:
    schema = connection.execute(
        "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
    ).fetchone()
    enabled_sources = tuple(
        row["id"] for row in connection.execute(
            "SELECT id FROM sources WHERE enabled = 1 ORDER BY id"
        )
    )
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
    return DoctorReport(
        hermes_version=hermes_version,
        git_commit=git_commit or _git_commit(Path(project_root)),
        schema_version=int(schema["schema_version"]) if schema else None,
        database_path=str(Path(database_path)),
        catalog_path=str(catalog), catalog_hash=_sha256(catalog),
        topics_path=str(topics), topics_hash=_sha256(topics),
        enabled_sources=enabled_sources,
        last_source_run_at=latest["timestamp"] if latest else None,
        user_skill_override=str(override) if is_override else None,
    )


def render_doctor_report(report: DoctorReport) -> str:
    sources = ", ".join(report.enabled_sources) or "none"
    override = report.user_skill_override or "none"
    return "\n".join([
        "Research Copilot doctor",
        f"Hermes version: {report.hermes_version}",
        f"Git commit: {report.git_commit}",
        f"Library schema: {report.schema_version}",
        f"Database: {report.database_path}",
        f"Source Catalog: {report.catalog_path} ({report.catalog_hash})",
        f"Topics: {report.topics_path} ({report.topics_hash})",
        f"Enabled sources: {sources}",
        f"Last source run: {report.last_source_run_at or 'never'}",
        f"User paper-skill override: {override}",
    ])
