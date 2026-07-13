"""Behavioural tests for gateway/task_draft.py — the interactive
/s add session state machine.

All tests mock the LLM via tools.task_extractor.set_llm_call_for_tests
so no network is touched.
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
    """Stack of canned responses — one per LLM call."""
    responses = list(payloads)
    def _call(system, user, model, base_url, api_key, timeout_s):
        return json.dumps(responses.pop(0), ensure_ascii=False) if responses else "{}"
    task_extractor.set_llm_call_for_tests(_call)


def test_start_or_advance_commits_on_full_first_turn():
    _canned({
        "fields": {"title": "跟 Bob 开会", "schedule_raw": "明天下午3点", "attendees": ["Bob"]},
        "next_prompt": None,
        "done": True,
        "confidence": 0.9,
    })
    r = task_draft.start_or_advance("u1", "明天下午3点跟 Bob 开会")
    assert r.committed is True
    assert r.cancelled is False
    assert "已添加" in r.text
    assert task_draft.has_active("u1") is False


def test_partial_input_opens_draft_and_asks():
    _canned({
        "fields": {"title": "跟 Alice 的会"},
        "next_prompt": "什么时间？",
        "done": False,
        "confidence": 0.6,
    })
    r = task_draft.start_or_advance("u1", "帮我加个跟 Alice 的会")
    assert r.committed is False
    assert "什么时间" in r.text
    assert task_draft.has_active("u1") is True
    d = task_draft.peek("u1")
    assert d.turn == 1
    assert d.fields["title"] == "跟 Alice 的会"


def test_second_turn_completes_and_commits():
    _canned(
        {"fields": {"title": "跟 Alice 的会"}, "next_prompt": "什么时间？",
         "done": False, "confidence": 0.6},
        {"fields": {"title": "跟 Alice 的会", "schedule_raw": "明天下午2点"},
         "next_prompt": None, "done": True, "confidence": 0.95},
    )
    r1 = task_draft.start_or_advance("u1", "帮我加个跟 Alice 的会")
    assert r1.committed is False
    r2 = task_draft.start_or_advance("u1", "明天下午2点")
    assert r2.committed is True
    assert "已添加" in r2.text
    assert task_draft.has_active("u1") is False


def test_seed_fields_persist_when_extractor_returns_partial():
    """If the CLI /s add call comes with --url flag, that url should still
    survive after the LLM asks a follow-up question."""
    _canned({
        "fields": {"title": "跟 Bob 开会", "url": "https://meet.example/abc"},
        "next_prompt": "什么时间？",
        "done": False,
        "confidence": 0.5,
    })
    r = task_draft.start_or_advance(
        "u1",
        "跟 Bob 开会",
        seed={"url": "https://meet.example/abc"},
    )
    assert r.committed is False
    d = task_draft.peek("u1")
    assert d.fields["url"] == "https://meet.example/abc"


def test_cancel_clears_draft():
    _canned({"fields": {"title": "t"}, "next_prompt": "when?", "done": False, "confidence": 0.5})
    task_draft.start_or_advance("u1", "add something")
    assert task_draft.has_active("u1") is True
    assert task_draft.cancel("u1") is True
    assert task_draft.has_active("u1") is False
    # Idempotent — second cancel returns False.
    assert task_draft.cancel("u1") is False


def test_extractor_cancel_flag_closes_draft():
    _canned(
        {"fields": {"title": "t"}, "next_prompt": "when?", "done": False, "confidence": 0.5},
        {"fields": {"cancel": True}, "next_prompt": None, "done": False, "confidence": 1.0},
    )
    task_draft.start_or_advance("u1", "add something")
    r = task_draft.start_or_advance("u1", "算了")
    assert r.cancelled is True
    assert task_draft.has_active("u1") is False


def test_llm_failure_keeps_draft_alive():
    def _fail(*a, **kw):
        raise TimeoutError("net down")
    task_extractor.set_llm_call_for_tests(_fail)
    r = task_draft.start_or_advance("u1", "帮我加个会")
    assert r.committed is False
    assert "无法解析" in r.text or "LLM" in r.text
    # Draft is still active so the user can retry.
    assert task_draft.has_active("u1") is True


def test_ttl_expiry_lazy_cleanup():
    from datetime import timedelta
    _canned({"fields": {"title": "t"}, "next_prompt": "when?", "done": False, "confidence": 0.5})
    task_draft.start_or_advance("u1", "hi")
    d = task_draft._drafts["u1"]
    d.last_touched = d.last_touched - timedelta(hours=1)  # force expiry
    assert task_draft.has_active("u1") is False
    assert "u1" not in task_draft._drafts


def test_sweep_returns_count():
    from datetime import timedelta
    _canned({"fields": {"title": "t"}, "next_prompt": "?", "done": False, "confidence": 0.5})
    task_draft.start_or_advance("u1", "one")
    _canned({"fields": {"title": "t"}, "next_prompt": "?", "done": False, "confidence": 0.5})
    task_draft.start_or_advance("u2", "two")
    for uid in ("u1", "u2"):
        task_draft._drafts[uid].last_touched -= timedelta(hours=1)
    assert task_draft.sweep() == 2
    assert not task_draft._drafts


def test_hard_slot_missing_asks_fallback_prompt_and_stays_open():
    """LLM says done=True but title/schedule_raw are still missing — the
    extractor demotes to done=False + asks a fallback question, and the
    draft module must NOT commit."""
    _canned({
        "fields": {"url": "https://x"},
        "next_prompt": None,
        "done": True,
        "confidence": 0.9,
    })
    r = task_draft.start_or_advance("u1", "https://x")
    assert r.committed is False
    assert task_draft.has_active("u1") is True
