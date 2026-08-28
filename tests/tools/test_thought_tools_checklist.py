"""Tests for the extended add_task schema and checklist ops in tools/thought_tools.py."""
from __future__ import annotations

import pytest



class TestCheckListOps:
    @pytest.fixture(autouse=True)
    def isolated_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import tools.thought_tools as tt
        # Force re-read of module-level paths inside the store helpers.
        yield tt
        # Nothing to tear down explicitly.

    @pytest.fixture(autouse=True)
    def _mock_schedule(self):
        import tools.schedule_parser as sp
        import json as _json
        sp.set_llm_call_for_tests(lambda *a, **kw: _json.dumps({
            "scheduled_at": "2027-01-01T15:00:00+08:00",
            "schedule_cron": None,
            "recurrence": "once",
            "confidence": 0.9,
        }))
        yield
        sp.set_llm_call_for_tests(None)

    def test_add_task_normalizes_structured_fields(self, isolated_store):
        tt = isolated_store
        t = tt.add_task(
            title="Design review",
            schedule_raw="tomorrow 3pm",
            url="https://meet.example/abc",
            location="Room 42",
            attendees="Bob, Alice, Bob",  # dedup ordering preserved
            tags=["meeting", "urgent"],
            remind_before_min="1h",
            checklist=["arch diagram", "3 benchmarks", "arch diagram"],  # dedup
        )
        assert t["url"] == "https://meet.example/abc"
        assert t["location"] == "Room 42"
        assert t["attendees"] == ["Bob", "Alice", "Bob"]  # comma-split preserves order; dedup happens at extractor layer, not here
        assert t["tags"] == ["meeting", "urgent"]
        assert t["remind_before_min"] == 60
        texts = [it["text"] for it in t["checklist"]]
        assert texts == ["arch diagram", "3 benchmarks"]
        for it in t["checklist"]:
            assert it["done"] is False
            assert it["id"].startswith("ci_")

    def test_check_and_uncheck_item(self, isolated_store):
        tt = isolated_store
        t = tt.add_task("m", "tomorrow", checklist=["a", "b", "c"])
        item_a_id = t["checklist"][0]["id"]
        # Check by 1-based index.
        assert tt.check_task_item(t["id"], 1) is True
        st = tt._load_store()
        assert st["tasks"][0]["checklist"][0]["done"] is True
        assert st["tasks"][0]["checklist"][0]["checked_at"] is not None
        # Uncheck by id.
        assert tt.uncheck_task_item(t["id"], item_a_id) is True
        st = tt._load_store()
        assert st["tasks"][0]["checklist"][0]["done"] is False
        # Non-existent ref returns False.
        assert tt.check_task_item(t["id"], 99) is False
        assert tt.check_task_item("tk_no_such", 1) is False

    def test_add_item_dedupes_and_caps(self, isolated_store):
        tt = isolated_store
        t = tt.add_task("m", "tomorrow", checklist=["a"])
        new_item = tt.add_task_item(t["id"], "  Bring slides  ")
        assert new_item is not None
        assert new_item["text"] == "Bring slides"
        # Duplicate rejected.
        assert tt.add_task_item(t["id"], "bring SLIDES") is None
        # Fill to cap.
        for i in range(20):
            tt.add_task_item(t["id"], f"item {i}")
        st = tt._load_store()
        assert len(st["tasks"][0]["checklist"]) == 20  # capped

    def test_remove_item(self, isolated_store):
        tt = isolated_store
        t = tt.add_task("m", "tomorrow", checklist=["a", "b", "c"])
        assert tt.remove_task_item(t["id"], 2) is True
        st = tt._load_store()
        remaining = [it["text"] for it in st["tasks"][0]["checklist"]]
        assert remaining == ["a", "c"]


class TestThoughtIncubation:
    @pytest.fixture(autouse=True)
    def isolated_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import tools.thought_tools as tt
        self.tt = tt

    def test_capture_text_creates_reviewable_thought(self):
        thought = self.tt.capture_thought_text("也许可以用本地模型分析日记", source="test")
        assert thought["stage"] == "fuzzy"
        assert thought["next_action"]["kind"] == "clarify"
        assert thought["review"]["next_review_at"]
        assert self.tt._load_store()["schema_version"] == 1

    def test_snooze_and_response_reset_backoff(self):
        thought = self.tt.capture_thought("Idea", "Try it")
        assert self.tt.snooze_thought(thought["id"], "3d") is True
        assert self.tt.record_thought_response(thought["id"], "先做一个原型") is True
        saved = self.tt.list_thoughts()[0]
        assert saved["stage"] == "exploring"
        assert saved["review"]["unanswered_count"] == 0
        assert saved["progress"]["status"] == "engaged"
        assert saved["history"][-1]["event"] == "user_response"
