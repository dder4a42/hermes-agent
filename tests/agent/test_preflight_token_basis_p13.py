"""P13: the preflight number that DECIDES and the number REPORTED share a basis.

The rough estimator is intentionally conservative — CJK text, dense JSON and
reasoning replay all cost it more than the provider bills — so a session can
show a rough estimate far above real usage. The preflight TRIGGER already
projects the last real provider reading forward by the growth the rough
estimator saw since that reading was paired, which keeps the compaction
decision honest. What was wrong is what got PUBLISHED: the log line and the
user-facing status message carried the raw rough number, so a correctly-fired
compaction read as a mis-calibrated one (~937,546 published for a request the
provider billed at 754,271) and "how much did compression reclaim" could not be
answered from the two numbers.

These pin the contract: one projection, read by the decision and by the
surface, with the documented fallbacks.
"""

from agent.context_compressor import ContextCompressor
from agent.turn_context import _preflight_surface_tokens


def _compressor(
    *,
    threshold: int = 750_000,
    real: int = 0,
    last_prompt: int | None = None,
    baseline: int = 0,
    awaiting: bool = False,
    compression_rough: int = 0,
) -> ContextCompressor:
    """A compressor shell for the pure projection/defer logic (no __init__).

    Built through the real constructor (the defer path reads provider-window
    state such as ``max_tokens`` / ``threshold_tokens_cap``), then
    ``threshold_tokens`` is pinned so the arithmetic under test is the
    projection rather than the window/threshold derivation.
    """
    c = ContextCompressor(
        model="test-model",
        threshold_percent=0.75,
        config_context_length=1_000_000,
        max_tokens=32_768,
    )
    c.threshold_tokens = threshold  # exact arithmetic under test; see docstring
    c.awaiting_real_usage_after_compression = awaiting
    c.last_real_prompt_tokens = real
    c.last_prompt_tokens = real if last_prompt is None else last_prompt
    c.last_rough_tokens_when_real_prompt_fit = baseline
    c.last_compression_rough_tokens = compression_rough
    return c


def test_projection_advances_the_real_reading_by_rough_growth():
    c = _compressor(real=754_271, baseline=935_000)

    assert c.projected_request_tokens(937_546) == (756_817, "provider")


def test_the_reported_number_is_the_decided_number():
    """The live session's numbers, which motivated the change.

    Rough 937,546; last real provider reading 754,271; paired rough baseline
    ~935,000. The compaction was CORRECT — the projected real usage sat above
    the 750,000 threshold — so the fix is to stop publishing the rough number as
    *the* size, not to change when compaction fires.
    """
    c = _compressor(real=754_271, baseline=935_000)
    rough = 937_546

    decided, basis = c.projected_request_tokens(rough)

    assert (decided, basis) == (756_817, "provider")
    assert c.should_defer_preflight_to_real_usage(rough) is False
    assert _preflight_surface_tokens(c, rough) == (decided, basis)
    assert decided < rough


def test_without_a_paired_reading_the_local_estimate_is_the_basis():
    c = _compressor(real=0, baseline=0)

    assert c.projected_request_tokens(120_000) == (120_000, "estimate")
    assert _preflight_surface_tokens(c, 120_000) == (120_000, "estimate")


def test_a_stale_reading_is_never_projected_onto_the_new_transcript():
    """After a compaction ``last_real_prompt_tokens`` predates the shorter history."""
    c = _compressor(real=754_271, baseline=935_000, awaiting=True)

    assert c.projected_request_tokens(60_000) == (60_000, "estimate")
    assert c.should_defer_preflight_to_real_usage(937_546) is True


def test_deferral_semantics_survive_the_refactor():
    """The four branches the projection replaced, one assertion each."""
    below_threshold = _compressor(threshold=750_000, real=600_000, baseline=897_546)
    assert below_threshold.should_defer_preflight_to_real_usage(700_000) is False
    assert below_threshold.should_defer_preflight_to_real_usage(937_546) is True

    already_over = _compressor(threshold=750_000, real=760_000, baseline=900_000)
    assert already_over.should_defer_preflight_to_real_usage(937_546) is False

    no_anchor = _compressor(threshold=750_000, real=0, baseline=0)
    assert no_anchor.should_defer_preflight_to_real_usage(937_546) is False

    compaction_baseline_only = _compressor(
        threshold=750_000, real=600_000, baseline=0, compression_rough=900_000
    )
    assert compaction_baseline_only.should_defer_preflight_to_real_usage(937_546) is True


def test_surface_tokens_fall_back_for_engines_without_the_projection():
    class Bare:
        pass

    assert _preflight_surface_tokens(Bare(), 5_000) == (5_000, "estimate")
