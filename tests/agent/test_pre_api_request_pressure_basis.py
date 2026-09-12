"""The loop's request-size numbers must use the FULL-REQUEST caliber.

``request_pressure_tokens`` (the pre-API pressure check) is the rough size of
the request the loop is about to send. It feeds ``should_compress`` and is
stashed via ``note_request_rough_estimate`` as the (rough, real) anchor that
``should_defer_preflight_to_real_usage`` projects from — so it has to cover the
same payload the provider bills: **system prompt + messages + tool schemas**.
A messages-only or transcript-only number understates the real request and can
let a session creep past the threshold with no output room left (#14695); the
dangerous direction is under-estimation.

Two call sites in ``agent/conversation_loop.py`` are pinned here:

* the pre-API check sizes ``api_messages``, which already carries the system
  prompt as its first element (prepended by "Build the final system message"),
  plus the tool schemas;
* the no-usage post-tool fallback sizes the durable transcript, which does NOT
  carry the system prompt, so it must pass it explicitly.

These are behavior contracts (what data the size is measured over), not source
snapshots: they run the real loop and inspect the real estimator inputs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from agent.model_metadata import (
    _estimate_tools_tokens_rough,
    estimate_messages_tokens_rough,
    estimate_request_tokens_rough,
    estimate_tokens_rough,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)


def _tool_call():
    return SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="web_search", arguments='{"query": "x"}'),
    )


def _tool_response(*, usage):
    message = SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[_tool_call()],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        model="test/model",
        usage=usage,
    )


def _final_response():
    message = SimpleNamespace(
        content="done",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


def _tool_definition():
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


def _history(n: int = 30):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
        for i in range(n)
    ]


def _short_history(n: int = 3):
    """Fewer messages than ``protect_first_n + protect_last_n + 1``.

    Keeps the turn prologue's preflight gate from running, so it does not seed
    ``last_prompt_tokens`` from its own rough estimate — the state a fresh /
    post-disconnect session is in when the loop's fallback must decide.
    """
    return _history(n)


def _make_agent(responses):
    """A real AIAgent with a scripted provider and a no-op compressor."""
    with (
        patch("run_agent.get_tool_definitions", return_value=[_tool_definition()]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=6,
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.side_effect = responses
    agent._cached_system_prompt = "You are Hermes Agent. " * 200
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.max_compression_attempts = 1

    compressor = MagicMock()
    compressor.protect_first_n = 3
    compressor.protect_last_n = 20
    compressor.threshold_tokens = 900_000
    compressor.context_length = 1_000_000
    compressor.last_prompt_tokens = 0
    compressor._verify_compaction_cleared_threshold = False
    compressor.awaiting_real_usage_after_compression = False
    compressor._ineffective_compression_count = 0
    compressor.should_compress.return_value = False
    compressor.should_compress_info.return_value = (False, None)
    compressor.should_compress_preflight.return_value = False
    compressor.should_defer_preflight_to_real_usage.return_value = False
    compressor.get_active_compression_failure_cooldown.return_value = None
    compressor.select_context.return_value = None
    compressor.get_automatic_compaction_status_message.return_value = ""

    def _update_from_response(usage):
        # No usage (disconnect / usage-less transport) leaves the reading at 0,
        # which is the branch that has to fall back to a local estimate.
        compressor.last_prompt_tokens = int((usage or {}).get("prompt_tokens", 0) or 0)

    compressor.update_from_response.side_effect = _update_from_response
    agent.compression_enabled = True
    agent.context_compressor = compressor
    return agent


def _run(agent, prompt: str, *, history=None, **extra_patches):
    ctx = (
        patch.object(agent, "_flush_messages_to_session_db", return_value=True),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    )
    with ctx[0], ctx[1], ctx[2], ctx[3]:
        return agent.run_conversation(
            prompt,
            conversation_history=_history() if history is None else history,
            **extra_patches,
        )


# ---------------------------------------------------------------------------
# Pre-API pressure check
# ---------------------------------------------------------------------------


def test_pre_api_pressure_payload_carries_the_system_prompt_and_tools():
    """Contract: the pre-API size is measured over system + messages + tools.

    ``api_messages`` is the assembled request, so the system prompt is inside
    the payload the estimator walks — it is not dropped (which would understate
    the request) and not passed twice (which would overstate it).
    """
    agent = _make_agent([_final_response()])
    payloads = []
    tool_payloads = []

    def _capture_messages(messages, **_kwargs):
        payloads.append(messages)
        return 200

    def _capture_tools(tools):
        tool_payloads.append(tools)
        return 0

    with (
        patch(
            "agent.conversation_loop.estimate_messages_tokens_rough",
            side_effect=_capture_messages,
        ),
        patch(
            "agent.conversation_loop._estimate_tools_tokens_rough",
            side_effect=_capture_tools,
        ),
    ):
        result = _run(agent, "hello")

    assert result["completed"] is True
    assert payloads, "the pre-API pressure check never sized the request"
    for payload in payloads:
        assert payload[0]["role"] == "system", (
            "the pre-API request size was measured over a payload with no "
            "system prompt — the real request is understated"
        )
        assert payload[0]["content"]
    assert tool_payloads, "tool schemas were excluded from the pre-API request size"


def test_a_transcript_only_size_understates_the_real_request():
    """Invariant: dropping the system prompt loses at least its own tokens.

    This is the magnitude of the #14695-class miss the pre-API check and the
    post-tool fallback must avoid.
    """
    system = "You are Hermes Agent. " * 400
    messages = [{"role": "user", "content": "hi"}]
    tools = [_tool_definition()] * 25

    full = estimate_request_tokens_rough(
        [{"role": "system", "content": system}] + messages, tools=tools
    )
    transcript_only = estimate_messages_tokens_rough(messages) + _estimate_tools_tokens_rough(
        tools
    )

    assert full - transcript_only >= estimate_tokens_rough(system)


# ---------------------------------------------------------------------------
# No-usage post-tool fallback
# ---------------------------------------------------------------------------


def test_no_usage_post_tool_fallback_includes_the_system_prompt():
    """Contract: when no provider reading is trusted, size the FULL request.

    The tool-loop tail falls back to a local estimate when
    ``last_prompt_tokens`` is 0 (API disconnect / usage-less transport). Its
    ``messages`` are the durable transcript, which carries no system prompt, so
    the estimate must pass one explicitly or the request is understated.
    """
    agent = _make_agent([_tool_response(usage=None), _final_response()])
    # Simulate the documented disconnect / usage-less transport: no provider
    # reading is trusted, so the fallback branch must size the request locally.
    agent.context_compressor.update_from_response.side_effect = (
        lambda _usage: setattr(agent.context_compressor, "last_prompt_tokens", 0)
    )
    calls = []

    def _capture(messages, system_prompt="", tools=None):
        calls.append(
            {"messages": messages, "system_prompt": system_prompt, "tools": tools}
        )
        return 200

    def _fake_execute_tool_calls(assistant_message, messages, *_args):
        tool_call = assistant_message.tool_calls[0]
        messages.append(
            {
                "role": "tool",
                "name": tool_call.function.name,
                "tool_call_id": tool_call.id,
                "content": "ok",
            }
        )

    with (
        patch(
            "agent.conversation_loop.estimate_request_tokens_rough",
            side_effect=_capture,
        ),
        patch.object(agent, "_execute_tool_calls", side_effect=_fake_execute_tool_calls),
    ):
        result = _run(agent, "do a lot of tool work", history=_short_history())

    assert result["completed"] is True
    assert calls, "the no-usage post-tool fallback never sized the request"
    assert any(c["system_prompt"] for c in calls), (
        "the no-usage post-tool fallback sized the request without the system "
        "prompt — the real request is understated; captured system_prompt "
        f"values: {[c['system_prompt'] for c in calls]}"
    )
    assert any(c["tools"] for c in calls), "tool schemas were excluded"
