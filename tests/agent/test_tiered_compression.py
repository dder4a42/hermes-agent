"""Tiered compression (board item P5): evict old tool-observation bodies first.

The compression pass is tiered:

* **Tier 1** replaces the BODIES of oversized old tool observations with bounded
  recovery markers. No LLM call. The message list's shape is preserved (same
  count, same roles, same order) so strict role alternation and the provider
  prompt structure survive. It runs first, and when it ALONE brings the request
  under the compression trigger the handoff is skipped entirely.
* **Tier 2** is the existing summarizer handoff, unchanged. It only runs when
  tier 1 alone did not clear the trigger.

The trigger decision rides the token count the PROVIDER reported for the last
call when one is available (it includes the system prompt and tool schemas the
local estimate misses), falling back to the local rough estimate.

Behavior contracts only: no source-text reads, no change-detector assertions.
Fixtures mirror tests/agent/test_proactive_tool_result_pruning.py and
tests/agent/test_proactive_prune_config.py.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from agent.context_compressor import (
    COMPRESSED_SUMMARY_METADATA_KEY,
    SUMMARY_PREFIX,
    ContextCompressor,
)
from agent.model_metadata import estimate_messages_tokens_rough
from hermes_state import SessionDB
from run_agent import AIAgent

# Small window: the compressor's small-context floor raises the effective
# threshold to 0.75 * context, so a 48K window triggers at 36K tokens.
CONTEXT = 48_000

# The fan-in report must survive BYTE-IDENTICAL. Distinctive and large enough
# that a bug which elided it would be impossible to miss.
FANIN_REPORT = (
    "Subagent fan-in report (delegate_task)\n"
    "claim: the storage migration is complete and verified\n"
    "evidence: alembic upgrade head -> exit 0; pytest tests/store -q -> 128 passed\n"
    + ("FANIN-REPORT-BODY-LINE\n" * 900)
)


def _compressor(**kw) -> ContextCompressor:
    defaults = dict(
        model="test",
        quiet_mode=True,
        threshold_percent=0.50,
        protect_first_n=0,
        protect_last_n=2,
        config_context_length=CONTEXT,
    )
    defaults.update(kw)
    # config_context_length short-circuits the probe; the patch is belt-and-
    # braces for the attributes the compressor derives on first access.
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=CONTEXT,
    ):
        return ContextCompressor(**defaults)


def _assistant_call(cid, name="terminal", args='{"cmd":"ls"}'):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": args}}
        ],
    }


def _tool_msg(cid, content):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _tool_by_id(msgs, cid):
    return [
        m for m in msgs
        if m.get("role") == "tool" and m.get("tool_call_id") == cid
    ][0]


def _index_of(msgs, cid):
    return next(
        i for i, m in enumerate(msgs)
        if m.get("role") == "tool" and m.get("tool_call_id") == cid
    )


# ~200KB of tool output across a realistic transcript: system + user, then an
# OLD subagent fan-in report followed by five bulky terminal observations.
_OBSERVATION_CHARS = 36_000
_TERMINAL_IDS = ["call_0", "call_1", "call_2", "call_3", "call_4"]


def _transcript():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "finish the migration"},
        _assistant_call("call_fanin", name="delegate_task"),
        _tool_msg("call_fanin", FANIN_REPORT),
    ]
    for i, cid in enumerate(_TERMINAL_IDS):
        msgs.append(_assistant_call(cid))
        msgs.append(_tool_msg(cid, chr(65 + i) * _OBSERVATION_CHARS))
    return msgs


def _spy_summary():
    """A recording stand-in for the tier-2 summarizer call."""
    calls = []

    def fake_summary(self, turns_to_summarize, focus_topic=None, memory_context=""):
        calls.append(turns_to_summarize)
        return "TIER-2 HANDOFF SUMMARY"

    return calls, fake_summary


# ── (a) the board item's acceptance case ────────────────────────────────────


def test_tier1_clears_trigger_and_preserves_structure_and_fan_in():
    c = _compressor(tier1_keep_recent_observations=2)
    msgs = _transcript()
    # Precondition: the transcript is genuinely over the compression trigger.
    assert estimate_messages_tokens_rough(msgs) > c.threshold_tokens

    with patch.object(
        ContextCompressor, "_generate_summary",
        side_effect=AssertionError("tier 2 must not run when tier 1 suffices"),
    ):
        result = c.compress(list(msgs))

    # Shape is preserved: same count, same roles, same order.
    assert len(result) == len(msgs)
    assert [m.get("role") for m in result] == [m.get("role") for m in msgs]
    # The body elision brought the estimated size below the trigger.
    assert estimate_messages_tokens_rough(result) < c.threshold_tokens
    # The fan-in report survived BYTE-IDENTICAL.
    fanin_idx = _index_of(msgs, "call_fanin")
    assert result[fanin_idx]["content"] == FANIN_REPORT
    # Old tool bodies were replaced by a bounded marker naming the recovery
    # route, not silently blanked.
    old_idx = _index_of(msgs, "call_0")
    assert result[old_idx]["content"] != msgs[old_idx]["content"]
    assert result[old_idx]["content"].startswith("[elided tool observation")
    assert "session_archive_search" in result[old_idx]["content"]


# ── (b) tier 1 is preferred: no handoff when it suffices ─────────────────────


def test_tier1_sufficient_skips_the_handoff_entirely():
    c = _compressor(tier1_keep_recent_observations=2)
    msgs = _transcript()
    calls, fake_summary = _spy_summary()

    with patch.object(ContextCompressor, "_generate_summary", fake_summary):
        result = c.compress(list(msgs))

    assert calls == []  # the expensive handoff never ran
    assert result is not msgs
    assert c._last_compression_made_progress is True
    # The /compress status readout reflects this tier-1 pass.
    assert c._last_compression_savings_pct > 0
    telemetry = c._last_compression_telemetry
    assert telemetry["tier1_evicted_observations"] >= 1
    assert telemetry["tier1_reclaimed_tokens"] > 0
    assert telemetry["tier1_tokens_source"] in {"provider", "estimate"}


# ── (c) tier 2 still runs when tier 1 is insufficient ───────────────────────


def test_tier2_handoff_runs_when_tier1_alone_is_insufficient():
    # Keep the newest four observations verbatim: only one old terminal body is
    # eligible, so tier 1 reclaims far less than the ~19K tokens needed to get
    # under the 36K trigger.
    c = _compressor(tier1_keep_recent_observations=4)
    msgs = _transcript()
    assert estimate_messages_tokens_rough(msgs) > c.threshold_tokens
    calls, fake_summary = _spy_summary()

    with patch.object(ContextCompressor, "_generate_summary", fake_summary):
        c.compress(list(msgs))

    assert calls, "tier 2 must run when tier 1 alone did not clear the trigger"
    # Tier 1 did run — it just was not enough.
    assert c._last_compression_telemetry["tier1_evicted_observations"] >= 1


# ── (d) the newest K observations survive verbatim ──────────────────────────


def test_newest_k_observations_survive_verbatim():
    c = _compressor(tier1_keep_recent_observations=2)
    msgs = _transcript()

    result, stats = c.evict_old_tool_observation_bodies(msgs)

    assert stats["evicted"] >= 1
    for cid in _TERMINAL_IDS[-2:]:  # the two newest observations
        assert _tool_by_id(result, cid)["content"] == _tool_by_id(msgs, cid)["content"]
    # An older observation WAS evicted, proving the boundary is real.
    assert _tool_by_id(result, "call_0")["content"] != _tool_by_id(msgs, "call_0")["content"]


# ── (e) provider-reported tokens win; local estimate is the fallback ────────


def test_authoritative_tokens_prefers_provider_reading_then_local_estimate():
    c = _compressor()
    c.last_real_prompt_tokens = 30_000
    assert c.authoritative_context_tokens() == (30_000, "provider")

    # No provider reading (first turn / aux path / test): local estimate.
    c.last_real_prompt_tokens = 0
    c.last_prompt_tokens = 0
    probe = [{"role": "user", "content": "x" * 400}]
    tokens, source = c.authoritative_context_tokens(probe)
    assert source == "estimate"
    assert tokens == estimate_messages_tokens_rough(probe)


def test_stale_post_compaction_reading_is_not_trusted():
    """After a committed compaction the parked reading is for the OLD transcript.

    The post-compression path sets ``awaiting_real_usage_after_compression`` and
    parks ``last_prompt_tokens = -1`` while ``last_real_prompt_tokens`` still
    holds the pre-compaction count. Projecting that stale, larger size onto the
    new shorter transcript would defeat tier 1, so the reading is ignored until
    the next real usage arrives (mirrors should_defer_preflight_to_real_usage).
    """
    c = _compressor()
    c.last_real_prompt_tokens = 999_999
    c.awaiting_real_usage_after_compression = True
    probe = [{"role": "user", "content": "short"}]
    tokens, source = c.authoritative_context_tokens(probe)
    assert source == "estimate"
    assert tokens == estimate_messages_tokens_rough(probe)


def test_provider_reading_wins_over_the_local_estimate_both_ways():
    msgs = _transcript()

    # Arm A — no provider reading: the local estimate says still over, so
    # tier 1 (one evictable body) is insufficient and tier 2 runs.
    c = _compressor(tier1_keep_recent_observations=4)
    calls, fake_summary = _spy_summary()
    with patch.object(ContextCompressor, "_generate_summary", fake_summary):
        c.compress(list(msgs))
    assert calls, "estimate arm: tier 1 insufficient → tier 2 must run"

    # Arm B — identical transcript and identical tier-1 work, but the provider
    # reported a tiny prompt for the last call, so tier 1 alone clears the
    # trigger and the handoff is skipped. Only the token SOURCE differs.
    c2 = _compressor(tier1_keep_recent_observations=4)
    c2.last_real_prompt_tokens = 10
    with patch.object(
        ContextCompressor, "_generate_summary",
        side_effect=AssertionError("provider reading must win"),
    ):
        result = c2.compress(list(msgs))
    assert result is not msgs

    # The local messages-only estimate is STILL over the trigger — proving the
    # provider reading, not the local estimate, drove the decision.
    assert estimate_messages_tokens_rough(result) > c2.threshold_tokens


def test_provider_reading_forces_tier2_where_the_local_estimate_would_not():
    c = _compressor(tier1_keep_recent_observations=2)
    msgs = _transcript()
    # The provider says the prompt is enormous (big system prompt + tool
    # schemas) even though the message-only estimate would clear the trigger
    # after tier 1.
    c.last_real_prompt_tokens = c.threshold_tokens + 500_000
    calls, fake_summary = _spy_summary()

    with patch.object(ContextCompressor, "_generate_summary", fake_summary):
        c.compress(list(msgs))

    assert calls, "provider reading dominates the local estimate in both directions"


# ── never-evict classes: fan-in, handoff/checkpoint, archive pointer ────────


def test_compression_handoff_and_archive_checkpoints_are_never_elided():
    # keep_recent=0 removes the recency protection entirely, so ONLY the
    # load-bearing rules can save these bodies.
    c = _compressor(tier1_keep_recent_observations=0)
    checkpoint = "[SESSION ARCHIVE CHECKPOINT]\n" + ("archived-chunk\n" * 2_000)
    summary_body = SUMMARY_PREFIX + "\n" + ("summary-body\n" * 2_000)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        _assistant_call("c1"), _tool_msg("c1", checkpoint),
        _assistant_call("c2"), _tool_msg("c2", "z" * 40_000),
        {
            "role": "assistant",
            "content": summary_body,
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        },
        _assistant_call("c3"), _tool_msg("c3", "w" * 40_000),
    ]

    result, stats = c.evict_old_tool_observation_bodies(msgs)

    assert stats["evicted"] >= 1  # the plain bodies were evicted
    assert _tool_by_id(result, "c1")["content"] == checkpoint
    assert result[6]["content"] == summary_body
    assert result[6].get(COMPRESSED_SUMMARY_METADATA_KEY) is True


# ── observability: the operator can see tier 1 working ─────────────────────


def test_tier1_eviction_is_observable_in_logs_and_telemetry(caplog):
    import logging

    c = _compressor(quiet_mode=False, tier1_keep_recent_observations=2)
    msgs = _transcript()
    with caplog.at_level(logging.INFO, logger="agent.context_compressor"):
        with patch.object(
            ContextCompressor, "_generate_summary",
            side_effect=AssertionError("tier 2 must not run"),
        ):
            c.compress(list(msgs))

    text = "\n".join(r.getMessage() for r in caplog.records)
    stats = c._last_compression_telemetry
    assert "Tier-1 tool-observation eviction cleared the trigger" in text
    assert f"evicted {stats['tier1_evicted_observations']}" in text
    assert str(stats["tier1_reclaimed_tokens"]) in text
    assert stats["tier1_tokens_source"] in {"provider", "estimate"}


# ── config surface: config.yaml keys with sane defaults and bounds ──────────


def test_tier1_knobs_have_defaults_and_upper_bounds():
    c = _compressor()
    assert c.tier1_keep_recent_observations == 4
    assert c.tier1_min_observation_chars == 2000

    clamped = _compressor(
        tier1_keep_recent_observations=100_000,
        tier1_min_observation_chars=1,
    )
    assert clamped.tier1_keep_recent_observations == 64  # upper bound

    assert clamped.tier1_min_observation_chars == 200  # _PRUNE_MIN_CHARS floor


def _config(**tier1_keys) -> dict:
    compression = {
        "enabled": True,
        "threshold": 0.50,
        "target_ratio": 0.20,
        "protect_first_n": 3,
        "protect_last_n": 20,
    }
    compression.update(tier1_keys)
    return {
        "compression": compression,
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }


def _make_agent(monkeypatch, tmp_path: Path, **tier1_keys) -> AIAgent:
    from hermes_cli import config as config_mod

    monkeypatch.setattr(config_mod, "load_config", lambda: _config(**tier1_keys))
    monkeypatch.setattr(
        config_mod, "load_config_readonly", lambda: _config(**tier1_keys)
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    with contextlib.redirect_stdout(io.StringIO()):
        return AIAgent(
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="test-key",
            provider="openai-codex",
            model="gpt-5.5",
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_memory=True,
            session_db=db,
            session_id="tier1-config-test",
        )


def test_tier1_config_keys_reach_the_compressor(monkeypatch, tmp_path):
    default_agent = _make_agent(monkeypatch, tmp_path)
    cc = default_agent.context_compressor
    assert cc.tier1_keep_recent_observations == 4
    assert cc.tier1_min_observation_chars == 2000

    custom = _make_agent(
        monkeypatch,
        tmp_path,
        tier1_keep_recent_observations=2,
        tier1_min_observation_chars=9_000,
    ).context_compressor
    assert custom.tier1_keep_recent_observations == 2
    assert custom.tier1_min_observation_chars == 9_000
