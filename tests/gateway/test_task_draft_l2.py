"""Additional tests for L2 title-rewriting / notes-checklist splitting
behaviour and input_raw persistence.

All extractor tests still mock the LLM — we're not testing that the
prompt makes deepseek-v4-flash do the right thing (that's a live smoke
below), we're testing that our code plumbs the extracted fields through
correctly regardless of how the LLM chose to structure them.
"""
from __future__ import annotations

import json

import pytest

from gateway import task_draft
from tools import task_extractor


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield


@pytest.fixture(autouse=True)
def _reset_drafts():
    task_draft._drafts.clear()
    yield
    task_draft._drafts.clear()
    task_extractor.set_llm_call_for_tests(None)


def _canned(*payloads):
    responses = list(payloads)
    def _call(system, user, model, base_url, api_key, timeout_s):
        return json.dumps(responses.pop(0), ensure_ascii=False) if responses else "{}"
    task_extractor.set_llm_call_for_tests(_call)


def test_title_normalized_stripped_prefix_survives_commit():
    _canned({
        "fields": {"title": "睡觉", "schedule_raw": "一小时后"},
        "next_prompt": None,
        "done": True,
        "confidence": 0.95,
    })
    r = task_draft.start_or_advance("u1", "提醒我一小时后睡觉")
    assert r.committed is True
    import json as _json
    from pathlib import Path
    import os
    home = os.environ["HERMES_HOME"]
    d = _json.loads(Path(home, "thoughts.json").read_text())
    t = d["tasks"][0]
    assert t["title"] == "睡觉"
    # Full user input preserved for audit.
    assert t["input_raw"] == "提醒我一小时后睡觉"


def test_notes_split_from_supplementary_context():
    _canned({
        "fields": {
            "title": "拿快递", "schedule_raw": "现在",
            "notes": "另一个包裹在保安室",
        },
        "next_prompt": None, "done": True, "confidence": 0.9,
    })
    r = task_draft.start_or_advance("u1", "去楼下拿快递，另一个包裹在保安室，现在就去")
    assert r.committed is True
    import json as _json, os
    from pathlib import Path
    d = _json.loads(Path(os.environ["HERMES_HOME"], "thoughts.json").read_text())
    t = d["tasks"][0]
    assert t["title"] == "拿快递"
    assert t["notes"] == "另一个包裹在保安室"
    assert "另一个包裹在保安室" not in t["title"]


def test_checklist_split_from_listed_items():
    _canned({
        "fields": {
            "title": "去超市买东西",
            "schedule_raw": "现在",
            "checklist": ["鸡蛋", "面粉", "糖"],
        },
        "next_prompt": None, "done": True, "confidence": 0.95,
    })
    r = task_draft.start_or_advance("u1", "去超市买鸡蛋、面粉、糖")
    assert r.committed is True
    import json as _json, os
    from pathlib import Path
    d = _json.loads(Path(os.environ["HERMES_HOME"], "thoughts.json").read_text())
    t = d["tasks"][0]
    assert t["title"] == "去超市买东西"
    texts = [it["text"] for it in t["checklist"]]
    assert texts == ["鸡蛋", "面粉", "糖"]


def test_input_raw_accumulates_across_turns():
    _canned(
        {"fields": {"title": "跟 Alice 开会"}, "next_prompt": "什么时候？",
         "done": False, "confidence": 0.6},
        {"fields": {"title": "跟 Alice 开会", "schedule_raw": "明天下午2点"},
         "next_prompt": None, "done": True, "confidence": 0.95},
    )
    task_draft.start_or_advance("u1", "帮我记一下跟 Alice 开会")
    r2 = task_draft.start_or_advance("u1", "明天下午2点")
    assert r2.committed is True
    import json as _json, os
    from pathlib import Path
    d = _json.loads(Path(os.environ["HERMES_HOME"], "thoughts.json").read_text())
    t = d["tasks"][0]
    # Both turns joined with " / " so we can audit the LLM's rewriting.
    assert "帮我记一下跟 Alice 开会" in t["input_raw"]
    assert "明天下午2点" in t["input_raw"]
    assert " / " in t["input_raw"]


def test_add_task_direct_call_with_input_raw_kwarg():
    """Non-draft callers (CLI/gateway fast path when all flags provided)
    can still pass input_raw explicitly."""
    import json as _json, os
    from pathlib import Path
    from tools import thought_tools
    from tools import schedule_parser
    schedule_parser.set_llm_call_for_tests(lambda *a, **k: _json.dumps({
        "scheduled_at": "2027-01-01T15:00:00+08:00",
        "schedule_cron": None, "recurrence": "once", "confidence": 0.9,
    }))
    task = thought_tools.add_task(
        title="Design review",
        schedule_raw="tomorrow 3pm",
        input_raw='"Design review" --when "tomorrow 3pm" --url X',
    )
    schedule_parser.set_llm_call_for_tests(None)
    assert task["input_raw"] == '"Design review" --when "tomorrow 3pm" --url X'


def test_add_task_default_input_raw_is_empty_string():
    """Legacy callers that don't pass input_raw must still work."""
    import json as _json
    from tools import thought_tools
    from tools import schedule_parser
    schedule_parser.set_llm_call_for_tests(lambda *a, **k: _json.dumps({
        "scheduled_at": "2027-01-01T15:00:00+08:00",
        "schedule_cron": None, "recurrence": "once", "confidence": 0.9,
    }))
    task = thought_tools.add_task(title="t", schedule_raw="tomorrow")
    schedule_parser.set_llm_call_for_tests(None)
    assert task["input_raw"] == ""
