"""Behavioural tests for scripts/task_surfacer.py — the no-agent cron script
that reads scheduled_at and emits reminder text."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CST = timezone(timedelta(hours=8))
SURFACER = Path(__file__).resolve().parent.parent.parent / "scripts" / "task_surfacer.py"


@pytest.fixture
def hermes_home(tmp_path):
    (tmp_path / "scripts").mkdir()
    return tmp_path


def _write_store(hermes_home: Path, tasks: list) -> Path:
    path = hermes_home / "thoughts.json"
    path.write_text(json.dumps({"schema_version": 1, "thoughts": [], "tasks": tasks}, ensure_ascii=False))
    return path


def _run_surfacer(hermes_home: Path) -> tuple[str, dict]:
    result = subprocess.run(
        [sys.executable, str(SURFACER)],
        env={"HERMES_HOME": str(hermes_home), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    store = json.loads((hermes_home / "thoughts.json").read_text())
    return result.stdout, store


def test_due_task_emits_reminder_and_marks_done(hermes_home):
    past = (datetime.now(CST) - timedelta(minutes=5)).isoformat(timespec="seconds")
    _write_store(hermes_home, [{
        "id": "tk_a", "title": "drink water", "schedule_raw": "5 min ago",
        "scheduled_at": past, "schedule_cron": "", "recurrence": "once",
        "state": "active", "created_at": "2026-07-13T00:00:00+08:00",
        "last_reminded": None, "lead_reminded_at": None, "remind_count": 0,
    }])
    stdout, store = _run_surfacer(hermes_home)
    assert "drink water" in stdout
    assert "⏰" in stdout
    task = store["tasks"][0]
    assert task["state"] == "done"
    assert task["remind_count"] == 1
    assert task["last_reminded"] is not None


def test_future_task_stays_silent(hermes_home):
    future = (datetime.now(CST) + timedelta(hours=2)).isoformat(timespec="seconds")
    _write_store(hermes_home, [{
        "id": "tk_b", "title": "future thing", "schedule_raw": "in 2h",
        "scheduled_at": future, "schedule_cron": "", "recurrence": "once",
        "state": "active", "created_at": "2026-07-13T00:00:00+08:00",
        "last_reminded": None, "lead_reminded_at": None, "remind_count": 0,
    }])
    stdout, store = _run_surfacer(hermes_home)
    assert stdout == ""
    assert store["tasks"][0]["state"] == "active"


def test_recurring_task_advances_scheduled_at(hermes_home):
    scheduled = (datetime.now(CST) - timedelta(minutes=1)).isoformat(timespec="seconds")
    _write_store(hermes_home, [{
        "id": "tk_c", "title": "weekly review", "schedule_raw": "weekly",
        "scheduled_at": scheduled, "schedule_cron": "0 14 * * 3",
        "recurrence": "weekly", "state": "active",
        "created_at": "2026-07-13T00:00:00+08:00",
        "last_reminded": None, "lead_reminded_at": None, "remind_count": 0,
    }])
    stdout, store = _run_surfacer(hermes_home)
    assert "weekly review" in stdout
    task = store["tasks"][0]
    assert task["state"] == "active"    # weekly stays active
    old_dt = datetime.fromisoformat(scheduled)
    new_dt = datetime.fromisoformat(task["scheduled_at"])
    assert new_dt - old_dt == timedelta(days=7)


def test_legacy_task_without_scheduled_at_stays_silent(hermes_home):
    """A pre-M0 task with scheduled_at=None must NOT trigger — that would
    fire every 5 min forever."""
    _write_store(hermes_home, [{
        "id": "tk_legacy", "title": "legacy", "schedule_raw": "someday",
        "scheduled_at": None, "schedule_cron": "", "recurrence": "once",
        "state": "active", "created_at": "2026-07-13T00:00:00+08:00",
        "last_reminded": None, "remind_count": 0,
    }])
    stdout, store = _run_surfacer(hermes_home)
    assert stdout == ""
    assert store["tasks"][0]["state"] == "active"


def test_paused_task_never_fires(hermes_home):
    past = (datetime.now(CST) - timedelta(minutes=10)).isoformat(timespec="seconds")
    _write_store(hermes_home, [{
        "id": "tk_p", "title": "paused", "schedule_raw": "...",
        "scheduled_at": past, "schedule_cron": "", "recurrence": "once",
        "state": "paused", "created_at": "2026-07-13T00:00:00+08:00",
        "last_reminded": None, "lead_reminded_at": None, "remind_count": 0,
    }])
    stdout, store = _run_surfacer(hermes_home)
    assert stdout == ""
    assert store["tasks"][0]["state"] == "paused"


def test_missing_store_is_silent(hermes_home):
    # no thoughts.json at all
    stdout, _ = subprocess.run(
        [sys.executable, str(SURFACER)],
        env={"HERMES_HOME": str(hermes_home), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=15,
    ), None
    # Actually assert on the CompletedProcess:
    assert stdout.returncode == 0
    assert stdout.stdout == ""


def test_task_without_absolute_schedule_never_fires(hermes_home):
    """The v1 schema has one source of truth: scheduled_at."""
    now = datetime.now(CST)
    cron = f"{now.minute} {now.hour} * * *"
    _write_store(hermes_home, [{
        "id": "tk_cron", "title": "cron legacy", "schedule_raw": "...",
        "scheduled_at": None, "schedule_cron": cron, "recurrence": "once",
        "state": "active", "created_at": "2026-07-13T00:00:00+08:00",
        "last_reminded": None, "lead_reminded_at": None, "remind_count": 0,
    }])
    stdout, store = _run_surfacer(hermes_home)
    assert stdout == ""
    assert store["tasks"][0]["state"] == "active"


def test_pre_reminder_fires_once_and_renders_checklist(hermes_home):
    """When now enters the [scheduled - remind_before, scheduled) window and
    lead_reminded_at is still null, the surfacer must emit a pre-reminder
    containing pending checklist items, then set lead_reminded_at so it
    doesn\'t fire twice."""
    upcoming = (datetime.now(CST) + timedelta(minutes=10)).isoformat(timespec="seconds")
    _write_store(hermes_home, [{
        "id": "tk_meet", "title": "Design review", "schedule_raw": "in 10 min",
        "scheduled_at": upcoming, "schedule_cron": "", "recurrence": "once",
        "state": "active", "created_at": "2026-07-13T00:00:00+08:00",
        "last_reminded": None, "lead_reminded_at": None, "remind_count": 0,
        "url": "https://meet.example/abc", "location": "Room 42",
        "attendees": ["Bob"], "tags": ["meeting"], "remind_before_min": 30,
        "checklist": [
            {"id": "ci_1", "text": "arch diagram", "done": False, "checked_at": None},
            {"id": "ci_2", "text": "benchmarks", "done": True, "checked_at": "..."},
        ],
    }])

    stdout, store = _run_surfacer(hermes_home)
    assert "Design review" in stdout
    # Pre-reminder wording, not the main "⏰ Reminder" one.
    assert "分钟" in stdout
    assert "arch diagram" in stdout
    # Done items are not shown.
    assert "benchmarks" not in stdout
    task = store["tasks"][0]
    assert task["lead_reminded_at"] is not None
    assert task["last_reminded"] is None
    assert task["state"] == "active"

    # Second run in the same window: no output, no double-fire.
    stdout2, _ = _run_surfacer(hermes_home)
    assert stdout2 == ""


def test_main_reminder_renders_structured_fields(hermes_home):
    past = (datetime.now(CST) - timedelta(minutes=2)).isoformat(timespec="seconds")
    _write_store(hermes_home, [{
        "id": "tk_meet", "title": "Design review", "schedule_raw": "just now",
        "scheduled_at": past, "schedule_cron": "", "recurrence": "once",
        "state": "active", "created_at": "2026-07-13T00:00:00+08:00",
        "last_reminded": None, "lead_reminded_at": None, "remind_count": 0,
        "url": "https://meet.example/abc", "location": "Room 42",
        "attendees": ["Bob", "Alice"], "tags": ["meeting"], "remind_before_min": 0,
        "checklist": [{"id": "ci_1", "text": "slides", "done": False, "checked_at": None}],
    }])
    stdout, store = _run_surfacer(hermes_home)
    assert "Design review" in stdout
    assert "https://meet.example/abc" in stdout
    assert "Room 42" in stdout
    assert "Bob" in stdout and "Alice" in stdout
    assert "slides" in stdout
    assert store["tasks"][0]["state"] == "done"


def test_pre_reminder_skipped_if_remind_before_zero(hermes_home):
    upcoming = (datetime.now(CST) + timedelta(minutes=1)).isoformat(timespec="seconds")
    _write_store(hermes_home, [{
        "id": "tk_a", "title": "quiet reminder", "schedule_raw": "soon",
        "scheduled_at": upcoming, "schedule_cron": "", "recurrence": "once",
        "state": "active", "created_at": "2026-07-13T00:00:00+08:00",
        "last_reminded": None, "lead_reminded_at": None, "remind_count": 0,
        "remind_before_min": 0,
    }])
    stdout, store = _run_surfacer(hermes_home)
    assert stdout == ""
    assert store["tasks"][0]["lead_reminded_at"] is None
