"""Unit tests for gateway.domain_discussion.

Tests exercise sticky-discussion state (load/save/expiry), the shared
subcommand dispatcher (ask/discuss/end), and the audit log — all with
_run_constrained_agent monkeypatched so the tests never hit a real model.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Reload get_hermes_home so any cached module-level path is re-derived.
    return home


@pytest.fixture
def stub_agent(monkeypatch):
    """Stub the constrained agent so tests don't need an LLM."""
    from gateway import domain_discussion as dd

    calls = []

    def _fake(system_prompt, user_message):
        calls.append({"system": system_prompt, "user": user_message})
        return f"[stub-answer] {user_message}"

    monkeypatch.setattr(dd, "_run_constrained_agent", _fake)
    return calls


def _write_thoughts_store(home: Path, data: dict) -> None:
    (home / "thoughts.json").write_text(json.dumps(data), encoding="utf-8")


def _write_paper_store(home: Path, files: dict) -> None:
    rc = home / "research-copilot"
    rc.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        path = rc / name
        if name.endswith(".jsonl"):
            path.write_text(
                "\n".join(json.dumps(r) for r in payload) + ("\n" if payload else ""),
                encoding="utf-8",
            )
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Active-discussion state
# ---------------------------------------------------------------------------


def test_no_active_discussion_returns_none(profile_home):
    from gateway.domain_discussion import load_active_discussion
    assert load_active_discussion() is None


def test_open_and_load_active_discussion(profile_home):
    from gateway.domain_discussion import (
        open_active_discussion, load_active_discussion,
    )

    disc = open_active_discussion("th", subject_id="th_abc", ttl_minutes=30)
    assert disc["domain"] == "th"
    assert disc["subject_id"] == "th_abc"
    assert disc["kind"] == "discuss"
    assert (profile_home / "active_discussion.json").exists()

    loaded = load_active_discussion()
    assert loaded is not None
    assert loaded["domain"] == "th"
    assert loaded["subject_id"] == "th_abc"


def test_expired_discussion_is_cleared_on_load(profile_home):
    from gateway.domain_discussion import (
        open_active_discussion, load_active_discussion,
    )

    disc = open_active_discussion("s", ttl_minutes=1)
    # Rewrite expires_at to the past.
    disc["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    (profile_home / "active_discussion.json").write_text(json.dumps(disc), encoding="utf-8")

    assert load_active_discussion() is None
    assert not (profile_home / "active_discussion.json").exists()


def test_corrupt_state_is_cleared(profile_home):
    from gateway.domain_discussion import load_active_discussion

    (profile_home / "active_discussion.json").write_text("{not json", encoding="utf-8")
    assert load_active_discussion() is None
    assert not (profile_home / "active_discussion.json").exists()


def test_append_turn_caps_history(profile_home):
    from gateway.domain_discussion import (
        open_active_discussion, append_active_discussion_turn,
        load_active_discussion,
    )

    disc = open_active_discussion("th")
    for i in range(30):
        append_active_discussion_turn(disc, f"q{i}", f"a{i}")

    loaded = load_active_discussion()
    assert len(loaded["turns"]) <= 20
    # The last kept turn is the most recent one.
    assert loaded["turns"][-1]["user"] == "q29"


def test_clear_active_discussion(profile_home):
    from gateway.domain_discussion import (
        open_active_discussion, clear_active_discussion, load_active_discussion,
    )

    open_active_discussion("paper")
    assert (profile_home / "active_discussion.json").exists()

    assert clear_active_discussion() is True
    assert load_active_discussion() is None

    # Idempotent — clearing again is a no-op that returns False.
    assert clear_active_discussion() is False


# ---------------------------------------------------------------------------
# End-intent detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "/end", "end", "END", "stop", "quit", "算了", "结束", "结束吧",
    "退出", "不聊了", "先这样", "好了", "不用了",
])
def test_end_intents_recognised(text):
    from gateway.domain_discussion import looks_like_end_intent
    assert looks_like_end_intent(text) is True


@pytest.mark.parametrize("text", [
    "我有个想法", "帮我看看我的日程", "hello", "论文推荐",
])
def test_non_end_text_not_recognised(text):
    from gateway.domain_discussion import looks_like_end_intent
    assert looks_like_end_intent(text) is False


# ---------------------------------------------------------------------------
# handle_domain_subcommand
# ---------------------------------------------------------------------------


def test_ask_returns_answer_and_opens_sticky(profile_home, stub_agent):
    _write_thoughts_store(profile_home, {"tasks": [], "thoughts": [
        {"id": "th_a", "title": "test thought", "state": "active"},
    ]})
    from gateway.domain_discussion import (
        handle_domain_subcommand, load_active_discussion,
    )
    reply = handle_domain_subcommand("th", "ask 我关心的想法有哪些")
    assert reply is not None
    assert "stub-answer" in reply
    assert len(stub_agent) == 1
    # A short sticky discussion should have been opened.
    disc = load_active_discussion()
    assert disc is not None
    assert disc["domain"] == "th"
    assert disc["kind"] == "ask"
    assert disc["turns"][-1]["user"] == "我关心的想法有哪些"


def test_discuss_with_subject_and_followup(profile_home, stub_agent):
    _write_thoughts_store(profile_home, {"tasks": [], "thoughts": [
        {"id": "th_a", "title": "focus on evidence chains", "state": "active"},
    ]})
    from gateway.domain_discussion import (
        handle_domain_subcommand, load_active_discussion,
    )
    reply = handle_domain_subcommand("th", "discuss th_a 帮我从这个想法出发展开")
    assert reply is not None
    assert "已开启" in reply
    disc = load_active_discussion()
    assert disc is not None
    assert disc["subject_id"] == "th_a"
    # First turn from the initial follow-up got logged.
    assert len(stub_agent) == 1
    assert disc["turns"][-1]["user"].startswith("帮我")


def test_discuss_without_subject(profile_home, stub_agent):
    _write_thoughts_store(profile_home, {"tasks": [], "thoughts": []})
    from gateway.domain_discussion import handle_domain_subcommand, load_active_discussion
    reply = handle_domain_subcommand("th", "discuss")
    assert reply is not None
    assert "已开启" in reply
    disc = load_active_discussion()
    assert disc is not None
    assert disc["subject_id"] is None
    # No agent call because no follow-up given.
    assert stub_agent == []


def test_end_closes_active_discussion(profile_home, stub_agent):
    from gateway.domain_discussion import (
        handle_domain_subcommand, open_active_discussion, load_active_discussion,
    )
    open_active_discussion("s")
    reply = handle_domain_subcommand("s", "end")
    assert reply is not None
    assert "已结束" in reply
    assert load_active_discussion() is None


def test_end_when_no_discussion(profile_home, stub_agent):
    from gateway.domain_discussion import handle_domain_subcommand
    reply = handle_domain_subcommand("s", "end")
    assert reply is not None
    assert "没有" in reply


def test_unknown_subcommand_returns_none(profile_home, stub_agent):
    from gateway.domain_discussion import handle_domain_subcommand
    assert handle_domain_subcommand("th", "list") is None
    assert handle_domain_subcommand("th", "") is None


def test_ask_without_question(profile_home, stub_agent):
    from gateway.domain_discussion import handle_domain_subcommand
    reply = handle_domain_subcommand("paper", "ask")
    assert reply is not None
    assert stub_agent == []  # never invoked the agent


# ---------------------------------------------------------------------------
# continue_active_discussion
# ---------------------------------------------------------------------------


def test_continue_active_discussion_returns_answer(profile_home, stub_agent):
    _write_thoughts_store(profile_home, {"tasks": [], "thoughts": []})
    from gateway.domain_discussion import (
        open_active_discussion, continue_active_discussion, load_active_discussion,
    )
    open_active_discussion("th", subject_id="th_a", ttl_minutes=30)
    reply = continue_active_discussion("我还想追问一下")
    assert reply == "[stub-answer] 我还想追问一下"
    disc = load_active_discussion()
    assert disc is not None
    assert disc["turns"][-1]["user"] == "我还想追问一下"


def test_continue_end_intent_clears_discussion(profile_home, stub_agent):
    from gateway.domain_discussion import (
        open_active_discussion, continue_active_discussion, load_active_discussion,
    )
    open_active_discussion("th")
    reply = continue_active_discussion("算了")
    assert reply is not None
    assert "已结束" in reply
    assert load_active_discussion() is None


def test_continue_returns_none_when_no_discussion(profile_home, stub_agent):
    from gateway.domain_discussion import continue_active_discussion
    assert continue_active_discussion("hello") is None


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_ask_writes_audit_entry(profile_home, stub_agent):
    _write_thoughts_store(profile_home, {"tasks": [], "thoughts": []})
    from gateway.domain_discussion import handle_domain_subcommand
    handle_domain_subcommand("th", "ask 论一下")
    log_path = profile_home / "discussions.jsonl"
    assert log_path.exists()
    entries = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["domain"] == "th"
    assert entry["kind"] == "ask"
    assert entry["question"] == "论一下"
    assert entry["answer"].startswith("[stub-answer]")


# ---------------------------------------------------------------------------
# Context builders — light smoke tests
# ---------------------------------------------------------------------------


def test_build_thoughts_context_partitions_by_state(profile_home):
    _write_thoughts_store(profile_home, {
        "tasks": [],
        "thoughts": [
            {"id": "th_a", "title": "A", "state": "active"},
            {"id": "th_b", "title": "B", "state": "dormant"},
            {"id": "th_c", "title": "C", "state": "archived"},
        ],
    })
    from gateway.domain_discussion import build_thoughts_context
    ctx = build_thoughts_context()
    assert [t["id"] for t in ctx["active_thoughts"]] == ["th_a"]
    assert [t["id"] for t in ctx["dormant_thoughts"]] == ["th_b"]
    assert [t["id"] for t in ctx["recent_archived_thoughts"]] == ["th_c"]


def test_build_schedule_context_partitions_tasks(profile_home):
    _write_thoughts_store(profile_home, {
        "tasks": [
            {"id": "tk_a", "title": "A", "state": "active"},
            {"id": "tk_b", "title": "B", "state": "done", "created_at": "2026-07-12T00:00Z"},
            {"id": "tk_c", "title": "C", "state": "archived", "created_at": "2026-07-11T00:00Z"},
        ],
        "thoughts": [],
    })
    from gateway.domain_discussion import build_schedule_context
    ctx = build_schedule_context()
    assert [t["id"] for t in ctx["active_tasks"]] == ["tk_a"]
    # Recent done tasks ordered newest first
    assert [t["id"] for t in ctx["recent_done_tasks"]] == ["tk_b", "tk_c"]


def test_build_paper_context_reads_snapshot(profile_home):
    _write_paper_store(profile_home, {
        "research_profile.json": {"long_term_agenda": [{"id": "agenda1"}]},
        "topics.json": {"topics": [{"id": "research-agent", "priority": 0.9}]},
        "source_registry.json": {"sources": [{"id": "openai"}]},
        "state.json": {"last_fetch_at": "2026-07-12T00:00Z"},
        "recommendations.jsonl": [
            {"id": "p1", "title": "T", "score": 0.9, "recommended_at": "2026-07-12"},
        ],
        "candidates.jsonl": [
            {"id": "p1", "type": "paper", "title": "T", "url": "u",
             "discovered_at": "2026-07-12T00:00Z", "status": "saved"},
        ],
        "interactions.jsonl": [
            {"kind": "save", "item_id": "p1", "at": "2026-07-12T09:00Z"},
        ],
    })
    from gateway.domain_discussion import build_paper_context
    ctx = build_paper_context(subject_id="p1")
    assert ctx["research_profile"]["long_term_agenda"][0]["id"] == "agenda1"
    assert ctx["topics"][0]["id"] == "research-agent"
    assert ctx["state"]["last_fetch_at"].startswith("2026-07-12")
    assert len(ctx["recent_recommendations"]) == 1
    assert len(ctx["saved_or_read_items"]) == 1
    # Subject is populated when the id matches.
    assert ctx["subject"]["id"] == "p1"
