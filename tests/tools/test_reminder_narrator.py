"""Tests for tools/reminder_narrator.py — the one-shot narrative line for
reminders.

The narrator itself is trivial (one LLM call + a text sanitizer). We
mock the call and cover: happy path, sanitisation, missing API key,
LLM exception, empty response, oversize response.
"""
from __future__ import annotations

import pytest

from tools import reminder_narrator


@pytest.fixture(autouse=True)
def _reset_hook(monkeypatch):
    # Clear any DEEPSEEK_API_KEY from env so the code path that looks it
    # up under HERMES_HOME/.env is exercised only where tests explicitly
    # set it.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    yield
    reminder_narrator.set_llm_call_for_tests(None)


def test_returns_narrative_line(monkeypatch):
    def _fake(system, user, model, base_url, api_key, timeout_s):
        return "你该睡觉了。\n(extra bleed)"
    reminder_narrator.set_llm_call_for_tests(_fake)
    line = reminder_narrator.narrate_reminder({"title": "睡觉"}, mode="main",
                                              api_key="x")
    assert line == "你该睡觉了。"


def test_missing_title_returns_none():
    line = reminder_narrator.narrate_reminder({"title": ""}, api_key="x")
    assert line is None


def test_missing_api_key_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # No .env, no DEEPSEEK_API_KEY in env, no test hook -> None.
    line = reminder_narrator.narrate_reminder({"title": "睡觉"})
    assert line is None


def test_env_file_supplies_key(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text('DEEPSEEK_API_KEY=env-key\n')
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    captured = {}
    def _fake(system, user, model, base_url, api_key, timeout_s):
        captured["api_key"] = api_key
        return "从 .env 拿到了。"
    reminder_narrator.set_llm_call_for_tests(_fake)
    line = reminder_narrator.narrate_reminder({"title": "睡觉"})
    assert line == "从 .env 拿到了。"
    assert captured["api_key"] == "env-key"


def test_llm_exception_returns_none():
    def _fake(*a, **kw):
        raise TimeoutError("net down")
    reminder_narrator.set_llm_call_for_tests(_fake)
    line = reminder_narrator.narrate_reminder({"title": "睡觉"}, api_key="x")
    assert line is None


def test_empty_response_returns_none():
    reminder_narrator.set_llm_call_for_tests(lambda *a, **kw: "")
    line = reminder_narrator.narrate_reminder({"title": "睡觉"}, api_key="x")
    assert line is None


def test_oversize_response_is_trimmed():
    long = "A" * 200
    reminder_narrator.set_llm_call_for_tests(lambda *a, **kw: long)
    line = reminder_narrator.narrate_reminder({"title": "x"}, api_key="x",
                                              max_chars=40)
    assert line is not None
    assert len(line) == 40
    assert line.endswith("…")


def test_wrapping_quotes_stripped():
    reminder_narrator.set_llm_call_for_tests(lambda *a, **kw: '"你该睡觉了"')
    line = reminder_narrator.narrate_reminder({"title": "睡觉"}, api_key="x")
    assert line == "你该睡觉了"


def test_pre_mode_prompts_include_minutes():
    """The pre-reminder path should feed minutes_left into the user prompt."""
    captured = {}
    def _fake(system, user, model, base_url, api_key, timeout_s):
        captured["system"] = system
        captured["user"] = user
        return "会前 30 分钟提醒。"
    reminder_narrator.set_llm_call_for_tests(_fake)
    reminder_narrator.narrate_reminder(
        {"title": "设计评审"},
        mode="pre",
        minutes_left=30,
        api_key="x",
    )
    assert "minutes_left: 30" in captured["user"]
    assert "BEFORE" in captured["system"] or "before" in captured["system"]


def test_main_mode_omits_minutes():
    captured = {}
    def _fake(system, user, model, base_url, api_key, timeout_s):
        captured["user"] = user
        return "会议开始了。"
    reminder_narrator.set_llm_call_for_tests(_fake)
    reminder_narrator.narrate_reminder(
        {"title": "设计评审"}, mode="main", api_key="x"
    )
    assert "minutes_left" not in captured["user"]


def test_first_context_hint_prefers_notes():
    captured = {}
    def _fake(system, user, model, base_url, api_key, timeout_s):
        captured["user"] = user
        return "OK"
    reminder_narrator.set_llm_call_for_tests(_fake)
    reminder_narrator.narrate_reminder(
        {"title": "拿快递", "notes": "另一个包裹在保安室",
         "attendees": ["Alice"], "location": "Building A"},
        mode="main", api_key="x",
    )
    # notes wins over attendees / location.
    assert "note: 另一个包裹在保安室" in captured["user"]
    assert "with:" not in captured["user"]
    assert "at:" not in captured["user"]


def test_pending_prep_items_signal_pre_mode():
    captured = {}
    def _fake(system, user, model, base_url, api_key, timeout_s):
        captured["user"] = user
        return "会前记得准备。"
    reminder_narrator.set_llm_call_for_tests(_fake)
    reminder_narrator.narrate_reminder(
        {"title": "设计评审",
         "checklist": [{"text": "a", "done": False},
                       {"text": "b", "done": True}]},
        mode="pre", minutes_left=30, api_key="x",
    )
    assert "pending_prep_items: 1" in captured["user"]
