"""Research Copilot profile bootstrap helpers.

These helpers are deliberately profile-home based so one Weixin bot can map to
one Hermes profile without sharing paper state, scripts, or cron jobs.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

_DEFAULT_JSON_FILES = {
    "config.json": '{\n  "pipeline": "research_signal_profile_aware"\n}\n',
    "topics.json": '{\n  "topics": []\n}\n',
    "source_registry.json": '{\n  "sources": []\n}\n',
    "research_profile.json": '{\n  "long_term_agenda": []\n}\n',
    "state.json": '{\n  "last_fetch_at": null,\n  "last_recommendation_at": null\n}\n',
}
_JSONL_FILES = ("candidates.jsonl", "recommendations.jsonl", "interactions.jsonl")
_SCRIPT_FILES = ("paper-fetch.py", "paper-health.py")
_CRON_NAMES = ("paper-fetcher", "daily-paper-pick", "paper-health-report")


def initialize_research_copilot_home(profile_home: str | Path, *, source_home: str | Path | None = None) -> dict[str, Any]:
    """Initialize a profile's Research Copilot files and optional scripts.

    Existing files are never overwritten. When ``source_home`` is supplied, the
    current profile's research-copilot JSON files and paper scripts are copied
    as templates for the new profile.
    """
    profile_home = Path(profile_home).expanduser().resolve()
    source = Path(source_home).expanduser().resolve() if source_home else None
    data_dir = profile_home / "research-copilot"
    scripts_dir = profile_home / "scripts"
    data_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    created: list[str] = []

    for filename, default_content in _DEFAULT_JSON_FILES.items():
        dst = data_dir / filename
        src = source / "research-copilot" / filename if source else None
        if dst.exists():
            continue
        if src and src.exists():
            shutil.copy2(src, dst)
            copied.append(f"research-copilot/{filename}")
        else:
            dst.write_text(default_content, encoding="utf-8")
            created.append(f"research-copilot/{filename}")

    for filename in _JSONL_FILES:
        dst = data_dir / filename
        if not dst.exists():
            dst.write_text("", encoding="utf-8")
            created.append(f"research-copilot/{filename}")

    for filename in _SCRIPT_FILES:
        dst = scripts_dir / filename
        src = source / "scripts" / filename if source else None
        if dst.exists() or not src or not src.exists():
            continue
        shutil.copy2(src, dst)
        copied.append(f"scripts/{filename}")

    return {
        "profile_home": str(profile_home),
        "data_dir": str(data_dir),
        "created": created,
        "copied": copied,
    }


def _existing_job_names() -> set[str]:
    from cron.jobs import list_jobs

    return {str(job.get("name") or "") for job in list_jobs(include_disabled=True)}


def install_research_copilot_cron(profile_home: str | Path, *, deliver: str = "weixin") -> dict[str, Any]:
    """Create the standard Research Copilot cron jobs in one profile store.

    Idempotent: existing jobs with the same names are left untouched.
    """
    from cron.jobs import create_job, use_cron_store

    profile_home = Path(profile_home).expanduser().resolve()
    initialize_research_copilot_home(profile_home)

    created: list[str] = []
    existing: list[str] = []

    with use_cron_store(profile_home):
        names = _existing_job_names()
        if "paper-fetcher" in names:
            existing.append("paper-fetcher")
        else:
            create_job(
                prompt=None,
                schedule="0 6,18 * * *",
                name="paper-fetcher",
                deliver=deliver,
                script="paper-fetch.py",
                no_agent=True,
            )
            created.append("paper-fetcher")

        if "daily-paper-pick" in names:
            existing.append("daily-paper-pick")
        else:
            create_job(
                prompt=(
                    "Select at most one high-signal Research Copilot paper/research signal "
                    "from the profile-local candidate pool. If nothing clears the threshold, stay silent."
                ),
                schedule="30 8 * * *",
                name="daily-paper-pick",
                deliver=deliver,
                skills=["paper"],
                enabled_toolsets=["file"],
            )
            created.append("daily-paper-pick")

        if "paper-health-report" in names:
            existing.append("paper-health-report")
        else:
            create_job(
                prompt=None,
                schedule="0 9 * * 6",
                name="paper-health-report",
                deliver=deliver,
                script="paper-health.py",
                no_agent=True,
            )
            created.append("paper-health-report")

    return {
        "profile_home": str(profile_home),
        "created": created,
        "existing": existing,
    }
