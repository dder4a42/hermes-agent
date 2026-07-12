"""Profile-scoped storage helpers for Research Copilot.

All paths are resolved through ``get_hermes_home()`` so each Hermes profile gets
its own candidate pool, recommendations, topics, and feedback history.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

DATA_DIR_NAME = "research-copilot"


def get_data_dir() -> Path:
    """Return the active profile's Research Copilot data directory."""
    return get_hermes_home() / DATA_DIR_NAME


def ensure_data_dir() -> Path:
    """Create and return the active profile's Research Copilot data directory."""
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(content, encoding="utf-8")


def load_topics() -> dict[str, Any]:
    """Load ``topics.json`` from the active profile."""
    return _read_json(get_data_dir() / "topics.json", {"topics": []})


def save_topics(data: dict[str, Any]) -> None:
    """Save ``topics.json`` under the active profile."""
    _write_json(get_data_dir() / "topics.json", data)


def load_config() -> dict[str, Any]:
    """Load ``config.json`` from the active profile."""
    return _read_json(get_data_dir() / "config.json", {})


def append_interaction(record: dict[str, Any]) -> None:
    """Append one feedback/action record to ``interactions.jsonl``."""
    path = ensure_data_dir() / "interactions.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_recommendations(limit: int = 10) -> list[dict[str, Any]]:
    """Return newest recommendations first."""
    rows = _read_jsonl(get_data_dir() / "recommendations.jsonl")
    rows.sort(key=lambda row: str(row.get("recommended_at") or ""), reverse=True)
    return rows[: max(0, limit)]


def find_recommendation(item_id: str) -> dict[str, Any] | None:
    """Find a recommendation by id."""
    item_id = str(item_id).strip()
    if not item_id:
        return None
    for row in _read_jsonl(get_data_dir() / "recommendations.jsonl"):
        if str(row.get("id") or "") == item_id:
            return row
    return None


def update_candidate_status(item_id: str, status: str) -> bool:
    """Update a candidate's status in ``candidates.jsonl``.

    Returns True when a matching candidate was found and rewritten.
    """
    item_id = str(item_id).strip()
    if not item_id:
        return False
    path = get_data_dir() / "candidates.jsonl"
    rows = _read_jsonl(path)
    changed = False
    for row in rows:
        if str(row.get("id") or "") == item_id:
            row["status"] = status
            changed = True
    if changed:
        _write_jsonl(path, rows)
    return changed
