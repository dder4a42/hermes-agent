"""Tests for hermes_cli.context_switch_guard."""

from __future__ import annotations

from types import SimpleNamespace

from hermes_cli.context_switch_guard import merge_preflight_compression_warning
from hermes_cli.model_switch import ModelSwitchResult


def _result(*, model: str = "small-model") -> ModelSwitchResult:
    return ModelSwitchResult(
        success=True,
        new_model=model,
        target_provider="openrouter",
        provider_changed=False,
        api_key="k",
        base_url="https://example.com/v1",
        api_mode="chat_completions",
        provider_label="openrouter",
        model_info={"context_length": 32_000},
    )


def _compressor(monkeypatch, *, context_length: int = 200_000):
    from agent.context_compressor import ContextCompressor

    monkeypatch.setattr(
        "agent.context_compressor.get_model_context_length",
        lambda *a, **k: context_length,
    )
    return ContextCompressor(
        model="big-model",
        threshold_percent=0.5,
        protect_first_n=3,
        protect_last_n=20,
        quiet_mode=True,
        config_context_length=context_length,
    )


# ---------------------------------------------------------------------------
# Projection basis (#P13 follow-up): the model-switch warning must judge the
# next turn on the SAME basis the preflight compaction decision uses. The
# preflight reads ContextCompressor.projected_request_tokens() — the last real
# provider reading advanced by the rough estimator's growth — so a rough
# estimate that runs high (CJK / dense JSON) no longer warns ~24% early.
# ---------------------------------------------------------------------------


def _projected_compressor(
    *,
    real: int,
    baseline: int,
    threshold_percent: float = 0.8,
    context_length: int = 1_000_000,
):
    """A real compressor with a paired (rough, real) anchor for the projection.

    ``threshold_percent=0.8`` against a 1M window pins the target model's
    auto-compress line at 800,000 — between the rough reading and its
    projection, so the basis choice flips the decision.
    """
    from agent.context_compressor import ContextCompressor

    cc = ContextCompressor(
        model="big-model",
        threshold_percent=threshold_percent,
        protect_first_n=3,
        protect_last_n=20,
        quiet_mode=True,
        config_context_length=context_length,
        max_tokens=32_768,
    )
    cc.awaiting_real_usage_after_compression = False
    cc.last_real_prompt_tokens = real
    cc.last_prompt_tokens = real if real > 0 else 0
    cc.last_rough_tokens_when_real_prompt_fit = baseline
    return cc


def _switch_agent(monkeypatch, cc, *, rough: int):
    """Agent shell whose request estimate is a fixed rough reading."""
    monkeypatch.setattr(
        "agent.model_metadata.estimate_request_tokens_rough",
        lambda *a, **k: rough,
    )
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard.resolve_display_context_length",
        lambda *a, **k: 1_000_000,
    )
    return SimpleNamespace(
        context_compressor=cc,
        compression_enabled=True,
        model="big-model",
        provider="openrouter",
        base_url="",
        api_key="",
    )


def _history(n: int = 30):
    # Must exceed protect_first_n + protect_last_n + 1 (24) so the estimator
    # is actually consulted.
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(n)
    ]


ROUGH = 937_546        # rough reading for the next request
REAL = 754_271         # last provider reading for the same conversation
BASELINE = 935_000     # rough reading paired with REAL
PROJECTED = REAL + (ROUGH - BASELINE)  # == 756,817
TARGET_THRESHOLD = 800_000  # 0.8 * 1,000,000


def test_no_early_warning_when_the_projected_request_fits(monkeypatch):
    """Contract: decide on the projection, not the raw rough estimate.

    The rough reading (937,546) clears the target model's 800,000 threshold
    while the projected real request (756,817) does not — the pre-fix guard
    warned here, prematurely.
    """
    assert ROUGH >= TARGET_THRESHOLD  # the pre-fix basis would have fired
    cc = _projected_compressor(real=REAL, baseline=BASELINE)
    assert cc.projected_request_tokens(ROUGH)[0] == PROJECTED < TARGET_THRESHOLD

    agent = _switch_agent(monkeypatch, cc, rough=ROUGH)
    result = _result()
    merge_preflight_compression_warning(result, agent=agent, messages=_history())

    assert not result.warning_message


def test_warning_reports_the_projected_number(monkeypatch):
    """When the projection does cross the line, the warning quotes it."""
    # ~902,546 real → over the 800,000 threshold.
    cc = _projected_compressor(real=900_000, baseline=BASELINE)
    projected = cc.projected_request_tokens(ROUGH)[0]
    assert projected >= TARGET_THRESHOLD

    agent = _switch_agent(monkeypatch, cc, rough=ROUGH)
    result = _result()
    merge_preflight_compression_warning(result, agent=agent, messages=_history())

    assert result.warning_message
    assert f"{projected:,}" in result.warning_message
    # The raw rough reading must not be published as the request size.
    assert f"{ROUGH:,}" not in result.warning_message


def test_falls_back_to_the_rough_estimate_without_a_paired_reading(monkeypatch):
    """First turn / no anchor: projection unavailable → rough basis is used."""
    cc = _projected_compressor(real=0, baseline=0)
    assert cc.projected_request_tokens(ROUGH) == (ROUGH, "estimate")

    agent = _switch_agent(monkeypatch, cc, rough=ROUGH)
    result = _result()
    merge_preflight_compression_warning(result, agent=agent, messages=_history())

    assert result.warning_message
    assert f"{ROUGH:,}" in result.warning_message


def test_falls_back_for_engine_without_the_projection(monkeypatch):
    """Older plugin engines / test doubles expose no projection method."""
    cc = SimpleNamespace(
        context_length=1_000_000,
        threshold_percent=0.8,
        protect_first_n=3,
        protect_last_n=20,
        _ineffective_compression_count=0,
        last_prompt_tokens=0,
        session_prompt_tokens=0,
    )
    assert not callable(getattr(cc, "projected_request_tokens", None))

    agent = _switch_agent(monkeypatch, cc, rough=ROUGH)
    result = _result()
    merge_preflight_compression_warning(result, agent=agent, messages=_history())

    assert result.warning_message
    assert f"{ROUGH:,}" in result.warning_message


def test_projection_raising_falls_back_to_the_rough_estimate(monkeypatch):
    """A broken projection must not crash the switch flow — use the estimate."""

    class _Exploding(SimpleNamespace):
        def projected_request_tokens(self, rough):
            raise RuntimeError("boom")

    cc = _Exploding(
        context_length=1_000_000,
        threshold_percent=0.8,
        protect_first_n=3,
        protect_last_n=20,
        _ineffective_compression_count=0,
        last_prompt_tokens=0,
        session_prompt_tokens=0,
    )
    agent = _switch_agent(monkeypatch, cc, rough=ROUGH)
    result = _result()
    merge_preflight_compression_warning(result, agent=agent, messages=_history())

    assert result.warning_message
    assert f"{ROUGH:,}" in result.warning_message


def test_merge_appends_to_existing_warning(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard._estimate_tokens",
        lambda *a, **k: 90_000,
    )
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard.resolve_display_context_length",
        lambda *a, **k: 32_000,
    )
    cc = _compressor(monkeypatch)
    agent = SimpleNamespace(
        context_compressor=cc,
        compression_enabled=True,
        base_url="",
        api_key="",
    )
    result = _result()
    result.warning_message = "expensive"
    merge_preflight_compression_warning(result, agent=agent)
    assert "expensive" in result.warning_message
    assert "preflight compression" in result.warning_message


def test_custom_provider_context_avoids_false_shrink_warning(monkeypatch):
    """Classic CLI used to omit custom_providers from the shrink warning.

    Repro: switch onto a custom endpoint with models.<id>.context_length=1M
    while session ~147k. Probe fails → hardcoded catalog match on "qwen"
    (131072) → false "Context window shrinks (... → 131,072)" warning, even
    though /model confirmation and the status bar correctly show 1M.
    """
    custom_provs = [
        {
            "name": "qwen-token-plan",
            "base_url": "https://token-plan.example/compatible-mode/v1",
            "models": {
                "qwen3.9-max-preview": {"context_length": 1_048_576},
            },
        }
    ]
    # Force the probe-down path that hit the "qwen" → 131072 catalog match
    # when custom_providers was not threaded through.
    monkeypatch.setattr(
        "agent.model_metadata._resolve_endpoint_context_length",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "agent.model_metadata._query_ollama_api_show",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "hermes_cli.context_switch_guard._estimate_tokens",
        lambda *a, **k: 147_053,
    )
    cc = _compressor(monkeypatch, context_length=1_000_000)
    agent = SimpleNamespace(
        model="MiniMax-M3",
        provider="minimax",
        context_compressor=cc,
        compression_enabled=True,
        conversation_history=[],
        base_url="https://api.minimax.example/v1",
        api_key="",
        _custom_providers=custom_provs,
    )
    result = ModelSwitchResult(
        success=True,
        new_model="qwen3.9-max-preview",
        target_provider="qwen-token-plan",
        provider_changed=True,
        api_key="k",
        base_url="https://token-plan.example/compatible-mode/v1",
        api_mode="chat_completions",
        provider_label="qwen-token-plan",
        model_info=None,
    )

    # Explicit custom_providers — no false shrink warning (1M > 147k*2).
    merge_preflight_compression_warning(
        result,
        agent=agent,
        custom_providers=custom_provs,
    )
    assert not result.warning_message

    # Agent snapshot alone (classic CLI historically forgot to pass the kwarg).
    result2 = ModelSwitchResult(
        success=True,
        new_model="qwen3.9-max-preview",
        target_provider="qwen-token-plan",
        provider_changed=True,
        api_key="k",
        base_url="https://token-plan.example/compatible-mode/v1",
        api_mode="chat_completions",
        provider_label="qwen-token-plan",
        model_info=None,
    )
    merge_preflight_compression_warning(result2, agent=agent)
    assert not result2.warning_message

    # Without any custom_providers source, catalog match still warns (131k).
    agent_no_cp = SimpleNamespace(
        model="MiniMax-M3",
        provider="minimax",
        context_compressor=cc,
        compression_enabled=True,
        conversation_history=[],
        base_url="https://api.minimax.example/v1",
        api_key="",
        _custom_providers=None,
    )
    result3 = ModelSwitchResult(
        success=True,
        new_model="qwen3.9-max-preview",
        target_provider="qwen-token-plan",
        provider_changed=True,
        api_key="k",
        base_url="https://token-plan.example/compatible-mode/v1",
        api_mode="chat_completions",
        provider_label="qwen-token-plan",
        model_info=None,
    )
    merge_preflight_compression_warning(result3, agent=agent_no_cp)
    assert result3.warning_message
    assert "preflight compression" in result3.warning_message
    assert "shrinks" in result3.warning_message
    # Must not honor the unused 1M custom override when no providers were passed.
    assert "1,048,576" not in result3.warning_message
