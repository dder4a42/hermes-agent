from __future__ import annotations

import json
from pathlib import Path


def test_migration_backs_up_and_preserves_ids(tmp_path):
    from scripts.migrate_thoughts import migrate

    path = tmp_path / "thoughts.json"
    path.write_text(json.dumps({
        "thoughts": [{"id": "th_old", "title": "idea"}],
        "tasks": [{"id": "tk_old", "title": "task", "schedule_raw": "tomorrow"}],
    }))
    backup, store = migrate(path)
    assert backup is not None and backup.exists()
    assert store["schema_version"] == 1
    assert store["thoughts"][0]["id"] == "th_old"
    assert store["thoughts"][0]["next_action"]["kind"] == "clarify"
    assert store["tasks"][0]["id"] == "tk_old"


def test_current_schema_is_noop(tmp_path):
    from scripts.migrate_thoughts import migrate

    path = tmp_path / "thoughts.json"
    path.write_text(json.dumps({"schema_version": 1, "thoughts": [], "tasks": []}))
    backup, _ = migrate(path)
    assert backup is None
