"""Tests for tools/task_extractor.py — the LLM slot-filler.

All tests mock the LLM to keep the suite offline + deterministic.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tools import task_extractor


@pytest.fixture(autouse=True)
def _reset_llm_hook():
    yield
    task_extractor.set_llm_call_for_tests(None)


CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 13, 22, 0, tzinfo=CST)


def _canned(payload):
    body = json.dumps(payload, ensure_ascii=False)
    def _call(system, user, model, base_url, api_key, timeout_s):
        return body
    task_extractor.set_llm_call_for_tests(_call)


def test_first_turn_full_sentence_marks_done():
    _canned({
        "fields": {
            "title": "跟 Bob 开设计评审",
            "schedule_raw": "明天下午3点",
            "attendees": ["Bob"],
        },
        "next_prompt": None,
        "done": True,
        "confidence": 0.95,
        "reasoning": "full sentence has title + schedule + attendee",
    })
    r = task_extractor.extract_fields("明天下午3点跟 Bob 开设计评审", now=NOW)
    assert r.ok is True
    assert r.done is True
    assert r.fields["title"] == "跟 Bob 开设计评审"
    assert r.fields["schedule_raw"] == "明天下午3点"
    assert r.fields["attendees"] == ["Bob"]
    assert r.next_prompt is None


def test_partial_input_asks_for_time():
    _canned({
        "fields": {"title": "跟 Alice 开会"},
        "next_prompt": "什么时间？",
        "done": False,
        "confidence": 0.6,
        "reasoning": "missing when",
    })
    r = task_extractor.extract_fields("帮我加个跟 Alice 的会", now=NOW)
    assert r.ok is True
    assert r.done is False
    assert r.next_prompt == "什么时间？"
    assert r.fields["title"] == "跟 Alice 开会"


def test_merges_new_field_into_existing_draft():
    _canned({
        "fields": {"schedule_raw": "明天下午2点"},
        "next_prompt": None,
        "done": True,
        "confidence": 0.9,
    })
    r = task_extractor.extract_fields(
        "明天下午2点",
        draft={"title": "跟 Alice 开会"},
        now=NOW,
        turn=2,
    )
    assert r.done is True
    assert r.fields["title"] == "跟 Alice 开会"
    assert r.fields["schedule_raw"] == "明天下午2点"


def test_force_done_at_max_turns():
    """If the LLM keeps asking past max_turns we commit anyway."""
    _canned({
        "fields": {"title": "some meeting"},
        "next_prompt": "URL?",
        "done": False,
        "confidence": 0.4,
    })
    r = task_extractor.extract_fields(
        "some meeting",
        draft={"schedule_raw": "tomorrow 3pm"},
        now=NOW,
        turn=5,
        max_turns=5,
    )
    assert r.done is True
    assert r.next_prompt is None


def test_done_true_but_missing_hard_slot_gets_demoted():
    """LLM said done but title is empty — parser must ask a follow-up."""
    _canned({
        "fields": {"schedule_raw": "明天下午3点"},
        "next_prompt": None,
        "done": True,
        "confidence": 0.7,
    })
    r = task_extractor.extract_fields("明天下午3点", now=NOW)
    assert r.done is False
    assert r.next_prompt is not None
    assert "内容" in r.next_prompt or "title" in r.next_prompt.lower()


def test_cancel_flag_propagates():
    _canned({
        "fields": {"cancel": True},
        "next_prompt": "已取消。",
        "done": False,
        "confidence": 1.0,
    })
    r = task_extractor.extract_fields(
        "算了不加了",
        draft={"title": "half-built"},
        now=NOW,
    )
    assert r.ok is True
    assert r.done is False
    # The extractor treats cancel like a terminal signal, wiping next_prompt.
    assert r.next_prompt is None
    assert r.fields.get("cancel") is True


def test_llm_raises_returns_error():
    def _call(*a, **kw):
        raise TimeoutError("read timed out")
    task_extractor.set_llm_call_for_tests(_call)
    r = task_extractor.extract_fields("明天下午3点", now=NOW)
    assert r.ok is False
    assert "TimeoutError" in r.error


def test_no_json_response_returns_error():
    def _call(*a, **kw):
        return "sorry I don't understand"
    task_extractor.set_llm_call_for_tests(_call)
    r = task_extractor.extract_fields("明天", now=NOW)
    assert r.ok is False
    assert "no json" in r.error.lower()


def test_empty_input_no_llm_call():
    called = {"n": 0}
    def _call(*a, **kw):
        called["n"] += 1
        return "{}"
    task_extractor.set_llm_call_for_tests(_call)
    r = task_extractor.extract_fields("   ", now=NOW)
    assert r.ok is True
    assert r.fields == {}
    assert called["n"] == 0


def test_lists_are_overwritten_not_appended():
    """Extractor is expected to return the FULL updated list, not deltas,
    so we don't accidentally double-add attendees."""
    _canned({
        "fields": {"attendees": ["Bob", "Alice"]},
        "next_prompt": None,
        "done": True,
        "confidence": 0.9,
    })
    r = task_extractor.extract_fields(
        "还有 Alice 也参加",
        draft={"title": "会议", "schedule_raw": "明天下午3点", "attendees": ["Bob"]},
        now=NOW,
        turn=3,
    )
    assert r.fields["attendees"] == ["Bob", "Alice"]


def test_code_fenced_json_is_parsed():
    def _call(*a, **kw):
        return "```json\n" + json.dumps({
            "fields": {"title": "t", "schedule_raw": "tomorrow"},
            "next_prompt": None,
            "done": True,
            "confidence": 0.9,
        }) + "\n```"
    task_extractor.set_llm_call_for_tests(_call)
    r = task_extractor.extract_fields("t tomorrow", now=NOW)
    assert r.ok is True
    assert r.done is True
    assert r.fields["title"] == "t"


def test_disallowed_field_stripped():
    """LLM shouldn't be able to sneak arbitrary keys into the draft."""
    _canned({
        "fields": {"title": "t", "schedule_raw": "tomorrow", "malicious_root_flag": True},
        "next_prompt": None,
        "done": True,
        "confidence": 0.9,
    })
    r = task_extractor.extract_fields("t tomorrow", now=NOW)
    assert r.ok is True
    assert "malicious_root_flag" not in r.fields
