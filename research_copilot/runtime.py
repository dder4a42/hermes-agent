"""Profile-scoped runtime assembly used by CLI and cron shims."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home

from .library import LibraryRepository, connect_library, initialize_library
from .library.database import SCHEMA_VERSION
from .sources.providers import build_provider_registry


def runtime_paths() -> dict[str, Path]:
    home = get_hermes_home()
    data = home / "research-copilot"
    research_config = data / "research-config.yaml"
    legacy_research_profile = data / "research-profile.yaml"
    if not research_config.exists() and legacy_research_profile.exists():
        research_config = legacy_research_profile
    return {
        "home": home,
        "data": data,
        "database": data / "library.db",
        "catalog": data / "sources.yaml",
        "topics": data / "topics.yaml",
        # The research config stores interests and beliefs; it is not a
        # Hermes execution profile. ``profile`` remains a compatibility alias.
        "research_config": research_config,
        "profile": research_config,
        "reports": data / "reports",
    }


def wiki_config() -> dict[str, Any]:
    """Return the profile-aware Research Wiki configuration.

    Behavioral settings live in ``config.yaml``.  The database and reports
    remain under the active ``HERMES_HOME`` so named profiles never share
    research state accidentally.
    """
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    research = config.get("research_copilot")
    if not isinstance(research, dict):
        research = {}
    wiki = research.get("wiki")
    if not isinstance(wiki, dict):
        wiki = {}
    configured_vault = str(wiki.get("vault_path") or "").strip()
    vault = Path(configured_vault).expanduser() if configured_vault else runtime_paths()["data"] / "wiki-vault"
    library_subdir = str(wiki.get("library_subdir") or "Research Library").strip()
    if not library_subdir or Path(library_subdir).is_absolute() or ".." in Path(library_subdir).parts:
        raise ValueError("research_copilot.wiki.library_subdir must be a relative directory")
    return {
        "vault_path": vault,
        "library_subdir": library_subdir,
    }


def scout_config() -> dict[str, Any]:
    """Return profile-isolated Hermes Scout behavior from config.yaml."""
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    research = config.get("research_copilot")
    if not isinstance(research, dict):
        research = {}
    scout = research.get("scout")
    if not isinstance(scout, dict):
        scout = {}
    profile = str(
        scout.get("execution_profile") or scout.get("profile") or "research-copilot"
    ).strip()
    timeout_seconds = int(scout.get("timeout_seconds", 2700))
    if not profile:
        raise ValueError("research_copilot.scout.profile must not be empty")
    if timeout_seconds < 60:
        raise ValueError("research_copilot.scout.timeout_seconds must be at least 60")
    return {
        "execution_profile": profile,
        "profile": profile,
        "timeout_seconds": timeout_seconds,
    }


def deep_research_config() -> dict[str, Any]:
    """Return isolated one-shot deep-research behavior from config.yaml."""
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    research = config.get("research_copilot")
    if not isinstance(research, dict):
        research = {}
    settings = research.get("deep_research")
    if not isinstance(settings, dict):
        settings = {}
    profile = str(
        settings.get("execution_profile") or settings.get("profile") or "research-copilot"
    ).strip()
    timeout_seconds = int(settings.get("timeout_seconds", 2700))
    if not profile:
        raise ValueError("research_copilot.deep_research.profile must not be empty")
    if timeout_seconds < 60:
        raise ValueError("research_copilot.deep_research.timeout_seconds must be at least 60")
    return {
        "execution_profile": profile,
        "profile": profile,
        "timeout_seconds": timeout_seconds,
    }


def ranking_config() -> dict[str, Any]:
    """Return validated ranking, triage, and promotion budgets."""
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    research = config.get("research_copilot")
    if not isinstance(research, dict):
        research = {}
    ranking = research.get("ranking")
    if not isinstance(ranking, dict):
        ranking = {}
    values = {
        "daily_triage_limit": int(ranking.get("daily_triage_limit", 10)),
        "daily_recommendation_limit": int(ranking.get("daily_recommendation_limit", 1)),
        "weekly_recommendation_limit": int(ranking.get("weekly_recommendation_limit", 3)),
        "triage_card_max_chars": int(ranking.get("triage_card_max_chars", 200)),
        "secondary_topic_bonus_cap": float(ranking.get("secondary_topic_bonus_cap", 0.10)),
    }
    for key in ("daily_triage_limit", "daily_recommendation_limit", "weekly_recommendation_limit"):
        if values[key] < 1:
            raise ValueError(f"research_copilot.ranking.{key} must be at least 1")
    if not 80 <= values["triage_card_max_chars"] <= 500:
        raise ValueError("research_copilot.ranking.triage_card_max_chars must be between 80 and 500")
    if not 0 <= values["secondary_topic_bonus_cap"] <= 0.25:
        raise ValueError("research_copilot.ranking.secondary_topic_bonus_cap must be between 0 and 0.25")
    return values


def collection_config() -> dict[str, Any]:
    """Return validated collection resilience settings from config.yaml."""
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    research = config.get("research_copilot")
    if not isinstance(research, dict):
        research = {}
    collection = research.get("collection")
    if not isinstance(collection, dict):
        collection = {}
    cooldown = collection.get("failure_cooldown")
    if not isinstance(cooldown, dict):
        cooldown = {}
    values = {
        "failure_threshold": int(cooldown.get("threshold", 3)),
        "failure_base_seconds": int(cooldown.get("base_minutes", 60)) * 60,
        "failure_max_seconds": int(cooldown.get("max_hours", 24)) * 3600,
    }
    if values["failure_threshold"] < 1:
        raise ValueError("research_copilot.collection.failure_cooldown.threshold must be at least 1")
    if values["failure_base_seconds"] < 60:
        raise ValueError("research_copilot.collection.failure_cooldown.base_minutes must be at least 1")
    if values["failure_max_seconds"] < values["failure_base_seconds"]:
        raise ValueError(
            "research_copilot.collection.failure_cooldown.max_hours must not be shorter than base_minutes"
        )
    return values


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Required Research Copilot config is missing: {path}")
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Research Copilot config must be a mapping: {path}")
    return value


def active_topics(path: Path) -> list[dict[str, Any]]:
    value = load_yaml(path)
    topics = value.get("topics")
    if not isinstance(topics, list):
        raise ValueError(f"topics must be a list: {path}")
    return [topic for topic in topics if isinstance(topic, dict) and topic.get("status", "active") == "active"]


def open_library(path: Path):
    from datetime import datetime, timezone
    import sqlite3

    connection = connect_library(path)
    now = datetime.now(timezone.utc)
    try:
        metadata_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_metadata'"
        ).fetchone() is not None
        if metadata_exists:
            row = connection.execute(
                "SELECT schema_version FROM schema_metadata WHERE singleton=1"
            ).fetchone()
            if row is not None and int(row["schema_version"]) < SCHEMA_VERSION:
                schema_version = int(row["schema_version"])
                backup_dir = Path(path).parent / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = now.strftime("%Y%m%dT%H%M%SZ")
                backup_path = backup_dir / f"library-schema-v{schema_version}-{stamp}.db"
                suffix = 1
                while backup_path.exists():
                    backup_path = backup_dir / f"library-schema-v{schema_version}-{stamp}-{suffix}.db"
                    suffix += 1
                destination = sqlite3.connect(backup_path)
                try:
                    connection.backup(destination)
                finally:
                    destination.close()
        initialize_library(connection, migrated_at=now.isoformat())
    except Exception:
        connection.close()
        raise
    return connection, LibraryRepository(connection)


def provider_registry():
    return build_provider_registry()
