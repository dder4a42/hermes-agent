"""Profile-scoped runtime assembly used by CLI and cron shims."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home

from .library import LibraryRepository, connect_library, initialize_library
from .sources.providers import build_provider_registry


def runtime_paths() -> dict[str, Path]:
    home = get_hermes_home()
    data = home / "research-copilot"
    return {
        "home": home,
        "data": data,
        "database": data / "library.db",
        "catalog": data / "sources.yaml",
        "topics": data / "topics.yaml",
        "profile": data / "research-profile.yaml",
    }


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

    connection = connect_library(path)
    initialize_library(connection, migrated_at=datetime.now(timezone.utc).isoformat())
    return connection, LibraryRepository(connection)


def provider_registry():
    return build_provider_registry()
