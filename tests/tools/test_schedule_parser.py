"""Tests for tools/schedule_parser.py — the LLM-driven schedule normaliser.

All tests mock the LLM call so no network is hit. The mock returns canned
JSON strings so each behavioural branch can be exercised in isolation.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tools import schedule_parser


@pytest.fixture(autouse=True)
def _reset_llm_hook():
    yield
    schedule_parser.set_llm_call_for_tests(None)


CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 13, 22, 0, tzinfo=CST)


def _canned(payload: dict):
    body = json.dumps(payload)
    def _call(system, user, model, base_url, api_key, timeout_s):
        return body
    schedule_parser.set_llm_call_for_tests(_call)


def test_parse_absolute_time_ok():
    _canned({
        "scheduled_at": "2026-07-14T15:00:00+08:00",
        "schedule_cron": None,
        "recurrence": "once",
        "confidence": 0.95,
        "reasoning": "tomorrow 3pm",
    })
    r = schedule_parser.parse_schedule("明天下午3点", now=NOW)
    assert r.ok is True
    assert r.scheduled_at == "2026-07-14T15:00:00+08:00"
    assert r.recurrence == "once"
    assert r.schedule_cron is None
    assert 0.94 < r.confidence < 0.96


def test_parse_recurring_populates_cron():
    _canned({
        "scheduled_at": "2026-07-15T14:00:00+08:00",
        "schedule_cron": "0 14 * * 3",
        "recurrence": "weekly",
        "confidence": 1.0,
        "reasoning": "every Wednesday 2pm",
    })
    r = schedule_parser.parse_schedule("每周三下午2点", now=NOW)
    assert r.ok is True
    assert r.recurrence == "weekly"
    assert r.schedule_cron == "0 14 * * 3"


def test_recurrence_once_drops_cron_even_if_llm_included_one():
    # Guardrail: LLM sometimes fills schedule_cron for one-shot events.
    # The parser must null that out so surfacers don't accidentally treat
    # a one-shot as recurring.
    _canned({
        "scheduled_at": "2026-07-14T15:00:00+08:00",
        "schedule_cron": "0 15 * * *",   # wrong for a one-shot
        "recurrence": "once",
        "confidence": 0.9,
    })
    r = schedule_parser.parse_schedule("明天下午3点", now=NOW)
    assert r.ok is True
    assert r.schedule_cron is None


def test_past_time_rejected():
    _canned({
        "scheduled_at": "2026-07-13T20:00:00+08:00",  # 2h before NOW
        "schedule_cron": None,
        "recurrence": "once",
        "confidence": 0.9,
    })
    r = schedule_parser.parse_schedule("下午8点", now=NOW)
    assert r.ok is False
    assert "past" in r.error.lower()


def test_far_future_rejected():
    _canned({
        "scheduled_at": "2030-01-01T00:00:00+08:00",  # >1 year out
        "schedule_cron": None,
        "recurrence": "once",
        "confidence": 0.9,
    })
    r = schedule_parser.parse_schedule("2030 年元旦", now=NOW)
    assert r.ok is False
    assert "horizon" in r.error.lower() or "exceed" in r.error.lower()


def test_invalid_iso_scheduled_at():
    _canned({
        "scheduled_at": "not-a-datetime",
        "schedule_cron": None,
        "recurrence": "once",
        "confidence": 0.5,
    })
    r = schedule_parser.parse_schedule("whenever", now=NOW)
    assert r.ok is False
    assert "invalid scheduled_at" in r.error.lower()


def test_llm_returns_no_json():
    def _call(*args, **kwargs):
        return "I don't think I can parse that."
    schedule_parser.set_llm_call_for_tests(_call)
    r = schedule_parser.parse_schedule("???", now=NOW)
    assert r.ok is False
    assert "no json" in r.error.lower()


def test_llm_raises_is_caught():
    def _call(*args, **kwargs):
        raise TimeoutError("read timed out")
    schedule_parser.set_llm_call_for_tests(_call)
    r = schedule_parser.parse_schedule("明天", now=NOW)
    assert r.ok is False
    assert "llm call failed" in r.error.lower()
    assert "TimeoutError" in r.error


def test_unknown_recurrence_coerced_to_once():
    _canned({
        "scheduled_at": "2026-07-14T10:00:00+08:00",
        "schedule_cron": None,
        "recurrence": "biweekly",  # not supported
        "confidence": 0.8,
    })
    r = schedule_parser.parse_schedule("每两周", now=NOW)
    assert r.ok is True
    assert r.recurrence == "once"


def test_empty_input_short_circuits_llm():
    called = {"count": 0}
    def _call(*args, **kwargs):
        called["count"] += 1
        return "{}"
    schedule_parser.set_llm_call_for_tests(_call)
    r = schedule_parser.parse_schedule("   ", now=NOW)
    assert r.ok is False
    assert called["count"] == 0  # no LLM traffic for empty input


def test_json_wrapped_in_code_fence():
    def _call(*args, **kwargs):
        return "```json\n" + json.dumps({
            "scheduled_at": "2026-07-14T15:00:00+08:00",
            "schedule_cron": None,
            "recurrence": "once",
            "confidence": 0.9,
        }) + "\n```"
    schedule_parser.set_llm_call_for_tests(_call)
    r = schedule_parser.parse_schedule("明天下午3点", now=NOW)
    assert r.ok is True
    assert r.scheduled_at == "2026-07-14T15:00:00+08:00"


def test_naive_scheduled_at_gets_tz_attached():
    _canned({
        "scheduled_at": "2026-07-14T15:00:00",  # no offset
        "schedule_cron": None,
        "recurrence": "once",
        "confidence": 0.8,
    })
    r = schedule_parser.parse_schedule("明天下午3点", now=NOW)
    assert r.ok is True
    # The parser normalises the naive datetime to the caller's tz.
    assert r.scheduled_at.startswith("2026-07-14T15:00:00")
    assert "+08:00" in r.scheduled_at
