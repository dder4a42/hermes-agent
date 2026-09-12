"""Providers doing in-path work must be able to publish forward progress.

Compression runs provider ``on_pre_compress`` hooks on the pass's critical path,
and the host abandons a pass whose commit fence has seen no progress for
``compression.context_timeout_seconds`` (default 120s). Before this, a provider
doing real work — chunk summaries that take seconds each — was indistinguishable
from a hung one: a 710-message pass was killed at 120s with ten summaries already
produced. ``progress_cb`` carries the provider's own signal, so a hung provider
still ticks nothing and still times out.
"""

import os
import threading
import time
from unittest.mock import MagicMock, patch

from agent.conversation_compression import CompressionCommitFence
from agent.memory_manager import MemoryManager
from agent.memory_provider import PRE_COMPRESS_CHECKPOINT_API_VERSION


class _TickingProvider:
    """Declares ``progress_cb`` and reports progress while it works."""

    name = "builtin"  # the builtin slot is always accepted by the manager
    pre_compress_checkpoint_api_version = PRE_COMPRESS_CHECKPOINT_API_VERSION

    def __init__(self, *, ticks=8, interval=0.05):
        self.ticks = ticks
        self.interval = interval
        self.ticked = 0

    def get_tool_schemas(self):
        return []

    def system_prompt_block(self):
        return ""

    def on_session_end(self, messages):
        return None

    def on_pre_compress(self, messages, *, progress_cb=None):
        for _ in range(self.ticks):
            time.sleep(self.interval)
            if progress_cb is not None:
                progress_cb()
                self.ticked += 1
        return ""


class _LegacyProvider:
    """Original one-argument signature — must keep being called that way."""

    name = "legacy"
    pre_compress_checkpoint_api_version = PRE_COMPRESS_CHECKPOINT_API_VERSION

    def __init__(self, *, ticks=8, interval=0.05):
        self.ticks = ticks
        self.interval = interval
        self.calls = 0

    def get_tool_schemas(self):
        return []

    def system_prompt_block(self):
        return ""

    def on_session_end(self, messages):
        return None

    def on_pre_compress(self, messages):
        self.calls += 1
        for _ in range(self.ticks):
            time.sleep(self.interval)
        return "legacy"


def test_manager_forwards_progress_cb_to_a_provider_that_declares_it():
    provider = _TickingProvider(ticks=3, interval=0.0)
    manager = MemoryManager()
    manager.add_provider(provider)
    ticks = []
    manager.on_pre_compress(
        [{"role": "user", "content": "hi"}], progress_cb=lambda: ticks.append(1)
    )
    assert provider.ticked == 3
    assert len(ticks) == 3


def test_manager_keeps_the_legacy_one_argument_signature():
    provider = _LegacyProvider(ticks=1, interval=0.0)
    manager = MemoryManager()
    manager.add_provider(provider)
    assert MemoryManager._provider_accepts_pre_compress_progress(provider) is False
    ticks = []
    result = manager.on_pre_compress(
        [{"role": "user", "content": "hi"}], progress_cb=lambda: ticks.append(1)
    )
    assert provider.calls == 1
    assert result == "legacy"
    assert ticks == [], "a provider that never ticks must not look like progress"


def _make_agent(tmp_path):
    from hermes_state import SessionDB
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "PRE_COMPRESS_PROGRESS"
    db.create_session(session_id, source="cli")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )
    compressor = MagicMock()
    compressor.compress.return_value = [{"role": "user", "content": "compressed"}]
    # The pass reads these as numbers/strings, so a bare MagicMock would blow up
    # on `>=` comparisons and print its own repr as a status line.
    compressor.compression_count = 0
    compressor.get_automatic_compaction_status_message.return_value = ""
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    agent._cached_system_prompt = "sys"
    return agent, compressor


def _run_with_monitor(agent, messages, fence):
    """Run a compression pass while sampling the fence's silence."""
    silence = []
    stop = threading.Event()

    def monitor():
        while not stop.is_set():
            silence.append(fence.seconds_since_progress())
            time.sleep(0.02)

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    try:
        agent._compress_context(messages, "sys", approx_tokens=120_000, commit_fence=fence)
    finally:
        stop.set()
        watcher.join(timeout=5)
    return max(silence) if silence else 0.0


def test_slow_provider_work_keeps_the_fence_fresh(tmp_path):
    """The pass must not look hung while a provider is demonstrably working."""
    agent, compressor = _make_agent(tmp_path)
    provider = _TickingProvider(ticks=15, interval=0.04)  # 0.6s of in-path work
    manager = MemoryManager()
    manager.add_provider(provider)
    agent._memory_manager = manager
    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

    worst_silence = _run_with_monitor(agent, messages, CompressionCommitFence())

    assert provider.ticked == 15
    assert compressor.compress.called, "the pass reached the engine"
    assert worst_silence < 0.35, (
        "provider ticks must reach the commit fence — the host abandons a pass "
        "after compression.context_timeout_seconds of silence"
    )


def test_slow_provider_that_cannot_tick_still_looks_hung(tmp_path):
    """Negative control: this is the provider's signal, not a keep-alive.

    A provider written against the old one-argument signature keeps working, but
    it cannot report progress — so a pass that spends all its time inside it
    still trips the watchdog. That is the behaviour the watchdog exists for.
    """
    agent, _compressor = _make_agent(tmp_path)
    provider = _LegacyProvider(ticks=15, interval=0.04)
    manager = MemoryManager()
    manager.add_provider(provider)
    agent._memory_manager = manager
    messages = [{"role": "user", "content": f"m{i}"} for i in range(20)]

    worst_silence = _run_with_monitor(agent, messages, CompressionCommitFence())

    assert provider.calls == 1
    assert worst_silence >= 0.35, "uncommunicated work must remain visible as silence"
