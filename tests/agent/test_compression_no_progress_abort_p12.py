"""P12: adaptive no-progress watchdog, same-turn retry, user-visible abort.

Three defects in the in-agent compression no-progress abort, pinned here as
BEHAVIOR CONTRACTS (how the pieces must relate), never as source snapshots:

1. **The watchdog is sized for the input.** A summary over a ~900K-token
   transcript can legitimately think for minutes before its first streamed
   token; the fixed 120s inactivity budget aborted real compressions that a
   later attempt completed in ~124s. ``resolve_context_compression_timeouts``
   now grows BOTH the idle budget and the total ceiling with the estimated
   input, so a slow-but-still-progressing summary of a huge transcript is not
   killed by a budget sized for a small one.
2. **The abort retried only on the next turn.** The observed stalls recovered
   ~2 minutes later — but only on the NEXT turn's preflight, so the session
   spent the intervening turns growing further past the threshold toward the
   hard provider token limit. The owned wrapper now retries ONCE, in the same
   turn.
3. **The abort was log/telemetry-only.** It now produces a user-visible
   FAILURE notice, single-sourced from
   ``COMPRESSION_NO_PROGRESS_ABORT_WARNING_TEMPLATE`` (whose gateway-noise
   survival is pinned in tests/gateway/test_telegram_noise_filter.py).

Contracts that must survive alongside: exactly one retry (no loop), no retry
after an explicit /stop, and no interference with a compression whose commit
is already in flight.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from agent.conversation_compression import (
    COMPRESSION_NO_PROGRESS_ABORT_WARNING_TEMPLATE,
    _clear_compression_no_progress_warning,
    _emit_compression_no_progress_warning,
    CompressionCommitFence,
    resolve_context_compression_timeouts,
    run_compress_context_with_progress_timeout,
)

ORIGINAL = [{"role": "user", "content": "keep-me"}]
COMPRESSED = [{"role": "user", "content": "summary of earlier turns"}]

# A budget small enough to abort in milliseconds, but scaled the same way the
# production default is (per-100K-token increments on top of a base).
FAST_CFG = {
    "context_timeout_seconds": 0.2,
    "context_total_ceiling_seconds": 0.3,
    "context_timeout_scale_per_100k_tokens_seconds": 0.2,
    "context_timeout_max_seconds": 600,
}
BIG_INPUT_TOKENS = 900_000


@pytest.fixture(autouse=True)
def _no_fallback_chain():
    """Keep the configured compression fallback chain inert.

    The chain retry (#78981) runs before the same-turn retry; pinning it empty
    makes these tests exercise the P12 path rather than the caller's real
    config.yaml.
    """
    with patch(
        "agent.auxiliary_client._get_auxiliary_task_config",
        return_value={"fallback_chain": []},
    ):
        yield


class _WarningRecorder:
    """Minimal agent stub exposing only the user-visible warning channel."""

    def __init__(self, *, hard_interrupt=False):
        self.warnings: list[str] = []
        self._hard_interrupt_requested = None
        if hard_interrupt:
            event = threading.Event()
            event.set()
            self._hard_interrupt_requested = event

    def _emit_warning(self, message: str) -> None:
        self.warnings.append(message)


class _StallingWorker:
    """Worker that streams nothing for its first ``stall_attempts`` runs.

    Mirrors the reported shape: the provider holds the connection open without
    emitting a token, so ``fence.touch_progress()`` never fires and the host's
    inactivity budget lapses.
    """

    def __init__(self, compressed, *, stall_attempts=1):
        self.compressed = compressed
        self.stall_attempts = stall_attempts
        self.attempts = 0
        self.fences: list[CompressionCommitFence] = []
        self._lock = threading.Lock()
        self.release = threading.Event()

    def __call__(self, fence: CompressionCommitFence):
        with self._lock:
            self.attempts += 1
            self.fences.append(fence)
            attempt = self.attempts
        if attempt <= self.stall_attempts:
            self.release.wait(timeout=5)
            return ([{"role": "assistant", "content": "late"}], "late-prompt")
        if not fence.begin_commit():
            return ([{"role": "assistant", "content": "cancelled"}], "cancelled")
        try:
            return (self.compressed, "summarized-prompt")
        finally:
            fence.finish_commit()


class _TickingWorker:
    """Slow but steadily progressing summary: a real tick every interval."""

    def __init__(self, compressed, *, ticks, interval):
        self.compressed = compressed
        self.ticks = ticks
        self.interval = interval
        self.attempts = 0

    def __call__(self, fence: CompressionCommitFence):
        self.attempts += 1
        for _ in range(self.ticks):
            time.sleep(self.interval)
            fence.touch_progress()
        if not fence.begin_commit():
            return (ORIGINAL, "aborted")
        try:
            return (self.compressed, "summary-prompt")
        finally:
            fence.finish_commit()


class _SilentThenCommitWorker:
    """No output for ``silence`` seconds, then the summary lands.

    The exact incident shape: a transient endpoint stall that recovers moments
    after the old fixed budget had already given up on it.
    """

    def __init__(self, compressed, *, silence):
        self.compressed = compressed
        self.silence = silence
        self.attempts = 0

    def __call__(self, fence: CompressionCommitFence):
        self.attempts += 1
        time.sleep(self.silence)
        if not fence.begin_commit():
            return (ORIGINAL, "aborted")
        try:
            return (self.compressed, "summary-prompt")
        finally:
            fence.finish_commit()


class _InFlightCommitWorker:
    """Reaches the commit boundary, then takes longer than the whole ceiling."""

    def __init__(self, compressed, *, commit_seconds):
        self.compressed = compressed
        self.commit_seconds = commit_seconds
        self.attempts = 0

    def __call__(self, fence: CompressionCommitFence):
        self.attempts += 1
        assert fence.begin_commit()
        try:
            time.sleep(self.commit_seconds)
            return (self.compressed, "committed-prompt")
        finally:
            fence.finish_commit()


# ---------------------------------------------------------------------------
# 1. Input-scaled watchdog
# ---------------------------------------------------------------------------


def test_owned_wrapper_sizes_the_watchdog_from_the_transcript(monkeypatch):
    """The in-agent host must hand its input estimate to the watchdog.

    Without this wiring the adaptive budget is dead code: the resolver's
    scaling only engages when the host passes an estimate, and no other test
    crosses that seam.
    """
    from unittest.mock import MagicMock

    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent.session_id = "s1"
    agent._cached_system_prompt = "sys"
    agent._conversation_root_id = MagicMock(return_value=None)

    seen: dict = {}

    def _fake_resolve(compression_cfg=None, estimated_tokens=None):
        seen["tokens"] = estimated_tokens
        return (0.05, 0.2)

    monkeypatch.setattr(
        "agent.conversation_compression.resolve_context_compression_timeouts",
        _fake_resolve,
    )
    monkeypatch.setattr(
        "agent.portal_tags.get_conversation_context", lambda: object()
    )

    def _fake_compress(agent_obj, messages, system_message, **kwargs):
        return (messages, "sys")

    monkeypatch.setattr(
        "agent.conversation_compression.compress_context", _fake_compress
    )

    AIAgent._compress_context(
        agent, [{"role": "user", "content": "x" * 4000}], "sys",
        approx_tokens=812_345,
    )
    assert seen["tokens"] == 812_345, (
        "the caller's own request estimate must reach the watchdog"
    )

    # No estimate supplied → the host measures the transcript rather than
    # falling back to an unscaled budget, and the measurement tracks size.
    seen.clear()
    AIAgent._compress_context(agent, [{"role": "user", "content": "x" * 4000}], "sys")
    small = seen["tokens"]
    seen.clear()
    AIAgent._compress_context(
        agent, [{"role": "user", "content": "x" * 400_000}], "sys"
    )
    assert small and small > 0
    assert seen["tokens"] > small


class TestAdaptiveWatchdogBudget:
    def test_budget_grows_with_estimated_input(self):
        base_idle, base_ceiling = resolve_context_compression_timeouts({})
        mid_idle, mid_ceiling = resolve_context_compression_timeouts({}, 200_000)
        big_idle, big_ceiling = resolve_context_compression_timeouts(
            {}, BIG_INPUT_TOKENS
        )

        assert big_idle > base_idle, "a huge summary must get a wider idle window"
        assert big_ceiling > base_ceiling, "and a wider total ceiling"
        assert base_idle < mid_idle < big_idle, "budget must scale monotonically"
        assert base_ceiling < mid_ceiling < big_ceiling
        # The widened idle window must still fit inside the widened ceiling, or
        # the ceiling would re-kill exactly the pass the idle window was
        # widened for.
        assert big_idle <= big_ceiling

    def test_no_estimate_keeps_the_historical_budget(self):
        assert resolve_context_compression_timeouts(
            {}, None
        ) == resolve_context_compression_timeouts({})
        assert resolve_context_compression_timeouts(
            {}, 0
        ) == resolve_context_compression_timeouts({})

    def test_scale_zero_restores_the_fixed_budget(self):
        cfg = dict(FAST_CFG, context_timeout_scale_per_100k_tokens_seconds=0)
        idle, ceiling = resolve_context_compression_timeouts(cfg, BIG_INPUT_TOKENS)
        assert idle == FAST_CFG["context_timeout_seconds"]
        assert ceiling == max(
            FAST_CFG["context_total_ceiling_seconds"],
            FAST_CFG["context_timeout_seconds"],
        )

    def test_scaled_idle_is_capped(self):
        cfg = dict(FAST_CFG, context_timeout_max_seconds=1.0)
        idle, ceiling = resolve_context_compression_timeouts(cfg, BIG_INPUT_TOKENS)
        assert idle == 1.0, "a bogus estimate must not grant an unbounded wait"
        assert ceiling >= idle

    def test_scaling_never_resurrects_a_disabled_watchdog(self):
        cfg = dict(FAST_CFG, context_timeout_seconds=0)
        idle, _ceiling = resolve_context_compression_timeouts(cfg, BIG_INPUT_TOKENS)
        assert idle == 0.0, "0 = disable the owned wrapper; scaling must not undo it"


class TestSlowSummariesSurviveTheWatchdog:
    def test_scaled_ceiling_keeps_a_slow_but_progressing_summary_alive(self):
        """Progress every 0.1s for 1.5s total — longer than the FIXED ceiling.

        The control run proves the historical budget really does abort this
        shape (so the test is about the fix, not about a shape that never
        failed); the scaled run proves the same progress is allowed to finish
        once the ceiling is sized for the input.
        """
        idle, ceiling = resolve_context_compression_timeouts(
            FAST_CFG, BIG_INPUT_TOKENS
        )
        assert idle > FAST_CFG["context_timeout_seconds"]
        assert ceiling > FAST_CFG["context_total_ceiling_seconds"]

        control = _TickingWorker(COMPRESSED, ticks=15, interval=0.1)
        control_msgs, _ = run_compress_context_with_progress_timeout(
            worker=control,
            messages=ORIGINAL,
            system_prompt_fallback="degraded",
            idle_timeout_seconds=FAST_CFG["context_timeout_seconds"],
            total_ceiling_seconds=FAST_CFG["context_total_ceiling_seconds"],
        )
        assert control_msgs is ORIGINAL, (
            "control: the fixed ceiling is what aborts a slow-but-progressing "
            "summary"
        )

        slow = _TickingWorker(COMPRESSED, ticks=15, interval=0.1)
        slow_msgs, slow_prompt = run_compress_context_with_progress_timeout(
            worker=slow,
            messages=ORIGINAL,
            system_prompt_fallback="degraded",
            idle_timeout_seconds=idle,
            total_ceiling_seconds=ceiling,
        )
        assert slow_msgs == COMPRESSED, (
            "a slow but steadily progressing summary must not be killed by a "
            "budget sized for a small transcript"
        )
        assert slow_prompt == "summary-prompt"

    def test_scaled_idle_budget_absorbs_a_silent_first_token_wait(self):
        """Silence longer than the fixed idle budget, then the summary lands."""
        idle, ceiling = resolve_context_compression_timeouts(
            FAST_CFG, BIG_INPUT_TOKENS
        )
        silence = FAST_CFG["context_timeout_seconds"] * 3

        control = _SilentThenCommitWorker(COMPRESSED, silence=silence)
        control_msgs, _ = run_compress_context_with_progress_timeout(
            worker=control,
            messages=ORIGINAL,
            system_prompt_fallback="degraded",
            idle_timeout_seconds=FAST_CFG["context_timeout_seconds"],
            total_ceiling_seconds=FAST_CFG["context_total_ceiling_seconds"],
        )
        assert control_msgs is ORIGINAL
        assert control.attempts == 1

        recovered = _SilentThenCommitWorker(COMPRESSED, silence=silence)
        recovered_msgs, recovered_prompt = (
            run_compress_context_with_progress_timeout(
                worker=recovered,
                messages=ORIGINAL,
                system_prompt_fallback="degraded",
                idle_timeout_seconds=idle,
                total_ceiling_seconds=ceiling,
            )
        )
        assert recovered_msgs == COMPRESSED, (
            "a transient stall inside the input-scaled budget must not be "
            "declared a no-progress abort"
        )
        assert recovered_prompt == "summary-prompt"
        assert recovered.attempts == 1, "no retry was needed"


# ---------------------------------------------------------------------------
# 2. Same-turn, bounded retry
# ---------------------------------------------------------------------------


class TestSameTurnRetry:
    def test_wrapper_default_stays_non_retrying(self):
        """Library callers keep the historical degrade; the host opts in."""
        worker = _StallingWorker(COMPRESSED)
        try:
            msgs, prompt = run_compress_context_with_progress_timeout(
                worker=worker,
                messages=ORIGINAL,
                system_prompt_fallback="degraded",
                idle_timeout_seconds=0.05,
                total_ceiling_seconds=0.2,
            )
        finally:
            worker.release.set()

        assert worker.attempts == 1
        assert msgs is ORIGINAL
        assert prompt == "degraded"

    def test_abort_retries_once_and_recovers_in_the_same_turn(self):
        worker = _StallingWorker(COMPRESSED)
        timeouts = []
        agent = _WarningRecorder()
        try:
            msgs, prompt = run_compress_context_with_progress_timeout(
                worker=worker,
                messages=ORIGINAL,
                system_prompt_fallback="degraded",
                idle_timeout_seconds=0.05,
                total_ceiling_seconds=0.2,
                on_timeout=lambda *args: timeouts.append(args),
                telemetry_agent=agent,
                retry_on_abort=True,
            )
        finally:
            worker.release.set()

        assert worker.attempts == 2, "the abort must be retried exactly once"
        assert msgs == COMPRESSED, "the retry's compression is published"
        assert prompt == "summarized-prompt"
        assert not timeouts, "no degrade report after a recovery"
        assert not agent.warnings, "a recovered attempt is not a user-facing failure"

    def test_retry_is_bounded_to_exactly_one_attempt(self):
        worker = _StallingWorker(COMPRESSED, stall_attempts=99)
        timeouts = []
        agent = _WarningRecorder()
        try:
            msgs, prompt = run_compress_context_with_progress_timeout(
                worker=worker,
                messages=ORIGINAL,
                system_prompt_fallback="degraded",
                idle_timeout_seconds=0.05,
                total_ceiling_seconds=0.2,
                on_timeout=lambda *args: timeouts.append(args),
                telemetry_agent=agent,
                retry_on_abort=True,
            )
        finally:
            worker.release.set()

        assert worker.attempts == 2, "the retry must never loop"
        assert msgs is ORIGINAL, "no messages may be dropped"
        assert prompt == "degraded"
        assert len(timeouts) == 1, "the degrade is reported exactly once"
        assert agent.warnings == [
            COMPRESSION_NO_PROGRESS_ABORT_WARNING_TEMPLATE
        ], "the user must be told once that compression aborted"

    def test_retry_runs_on_a_fresh_fence(self):
        worker = _StallingWorker(COMPRESSED)
        minted = []

        def _new_fence():
            fence = CompressionCommitFence()
            minted.append(fence)
            return fence

        try:
            msgs, _prompt = run_compress_context_with_progress_timeout(
                worker=worker,
                messages=ORIGINAL,
                system_prompt_fallback="degraded",
                idle_timeout_seconds=0.05,
                total_ceiling_seconds=0.2,
                new_fence=_new_fence,
                retry_on_abort=True,
            )
        finally:
            worker.release.set()

        assert msgs == COMPRESSED
        assert len(minted) == 1, "exactly one fence is minted for the one retry"
        assert worker.fences[1] is minted[0]
        assert worker.fences[1] is not worker.fences[0]
        assert worker.fences[0].is_cancelled, "the aborted attempt stays cancelled"

    def test_explicit_stop_suppresses_the_retry(self):
        """An explicit /stop is not a stalled route."""
        worker = _StallingWorker(COMPRESSED)
        agent = _WarningRecorder(hard_interrupt=True)
        timeouts = []
        try:
            msgs, prompt = run_compress_context_with_progress_timeout(
                worker=worker,
                messages=ORIGINAL,
                system_prompt_fallback="degraded",
                idle_timeout_seconds=0.05,
                total_ceiling_seconds=0.2,
                on_timeout=lambda *args: timeouts.append(args),
                telemetry_agent=agent,
                retry_on_abort=True,
            )
        finally:
            worker.release.set()

        assert worker.attempts == 1
        assert msgs is ORIGINAL
        assert prompt == "degraded"
        assert len(timeouts) == 1

    def test_in_flight_commit_is_never_abandoned_or_retried(self):
        """Commit already in flight → wait for it, publish it, do NOT retry."""
        worker = _InFlightCommitWorker(COMPRESSED, commit_seconds=0.2)

        msgs, prompt = run_compress_context_with_progress_timeout(
            worker=worker,
            messages=ORIGINAL,
            system_prompt_fallback="degraded",
            idle_timeout_seconds=0.05,
            total_ceiling_seconds=0.05,
            retry_on_abort=True,
        )

        assert worker.attempts == 1, "an in-flight commit must not be retried"
        assert msgs == COMPRESSED
        assert prompt == "committed-prompt"


# ---------------------------------------------------------------------------
# 3. The abort notice reaches the user, exactly once per failure
# ---------------------------------------------------------------------------


class TestNoProgressAbortNotice:
    def test_notice_is_emitted_once_and_deduped(self):
        agent = _WarningRecorder()

        _emit_compression_no_progress_warning(agent)
        _emit_compression_no_progress_warning(agent)

        assert agent.warnings == [COMPRESSION_NO_PROGRESS_ABORT_WARNING_TEMPLATE]

    def test_notice_re_arms_after_a_completed_compaction(self):
        agent = _WarningRecorder()

        _emit_compression_no_progress_warning(agent)
        _clear_compression_no_progress_warning(agent)
        _emit_compression_no_progress_warning(agent)

        assert agent.warnings == [
            COMPRESSION_NO_PROGRESS_ABORT_WARNING_TEMPLATE,
            COMPRESSION_NO_PROGRESS_ABORT_WARNING_TEMPLATE,
        ]

    def test_notice_is_fail_soft_without_a_warning_channel(self):
        class _Bare:
            pass

        _emit_compression_no_progress_warning(_Bare())  # must not raise
