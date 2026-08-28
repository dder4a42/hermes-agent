from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SURFACER = Path(__file__).resolve().parent.parent.parent / "scripts" / "thought_surfacer.py"


def _run(home: Path) -> tuple[str, dict]:
    result = subprocess.run(
        [sys.executable, str(SURFACER)],
        env={"HERMES_HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout, json.loads((home / "thoughts.json").read_text())


def test_due_thought_surfaces_action_and_backs_off(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "thoughts:\n  incubation:\n    active_hours: {start: '00:00', end: '23:59'}\n"
    )
    thought = {
        "id": "th_one", "title": "Local journal", "summary": "Find patterns",
        "source": "chat", "tags": [], "state": "active", "stage": "fuzzy",
        "priority": "normal", "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(), "surfaced_at": None,
        "surface_count": 0, "open_questions": [],
        "next_action": {"kind": "track", "prompt": "原型有进展吗？"},
        "review": {"next_review_at": (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),
                   "cadence": "7d", "snoozed_until": None, "unanswered_count": 0},
        "progress": {"status": "unexplored", "last_update_at": None}, "history": [],
    }
    (tmp_path / "thoughts.json").write_text(json.dumps({"schema_version": 1, "thoughts": [thought], "tasks": []}))
    stdout, store = _run(tmp_path)
    assert "Local journal" in stdout
    assert "原型有进展吗" in stdout
    saved = store["thoughts"][0]
    assert saved["surface_count"] == 1
    assert saved["review"]["unanswered_count"] == 1
    assert datetime.fromisoformat(saved["review"]["next_review_at"]) > datetime.now(timezone.utc)


def test_future_or_snoozed_thought_stays_silent(tmp_path):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    thought = {"id": "th_two", "state": "active", "review": {"next_review_at": future, "snoozed_until": future}}
    (tmp_path / "thoughts.json").write_text(json.dumps({"schema_version": 1, "thoughts": [thought], "tasks": []}))
    stdout, _ = _run(tmp_path)
    assert stdout == ""
