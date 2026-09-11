import json
from types import SimpleNamespace

from plugins.memory.session_archive import SessionArchiveProvider


def test_session_archive_provider_loads_by_name():
    from plugins.memory import load_memory_provider

    provider = load_memory_provider("session_archive", register_skills=False)
    assert provider is not None
    assert provider.name == "session_archive"
    names = [schema["name"] for schema in provider.get_tool_schemas()]
    assert "session_archive_get_claims" in names


def test_pre_compress_archives_chunks_and_manifest(tmp_path):
    provider = SessionArchiveProvider()
    provider.initialize("session:one", hermes_home=str(tmp_path))

    manifest = provider.on_pre_compress([
        {"role": "user", "content": "Investigate SJTU provider routing."},
        {"role": "tool", "name": "curl", "content": "Connected to models.sjtu.edu.cn"},
        {"role": "assistant", "content": "Claim: VPN route works. Evidence: curl connected."},
    ])

    assert "[SESSION ARCHIVE CHECKPOINT]" in manifest
    assert "chk-" in manifest
    search = provider.search("models.sjtu.edu.cn")
    assert search["results"]
    expanded = provider.expand(search["results"][0]["chunk_id"])
    assert "VPN route works" in expanded["content"]


def test_tools_search_expand_and_claims(tmp_path):
    provider = SessionArchiveProvider()
    provider.initialize("parent", hermes_home=str(tmp_path))
    provider.on_pre_compress([{"role": "user", "content": "Need claim evidence for compression."}])
    provider.on_delegation(
        "Check implementation",
        "Claim: tests pass\nEvidence: pytest returned green",
        child_session_id="child-1",
    )

    search = json.loads(provider.handle_tool_call("session_archive_search", {"query": "claim evidence"}))
    assert search["results"][0]["chunk_id"].startswith("chk-")

    expanded = json.loads(provider.handle_tool_call(
        "session_archive_expand",
        {"chunk_id": search["results"][0]["chunk_id"], "max_chars": 2000},
    ))
    assert "Need claim evidence" in expanded["content"]

    claims = json.loads(provider.handle_tool_call("session_archive_get_claims", {}))
    assert claims["reports"][0]["child_session_id"] == "child-1"


def test_llm_chunk_summary_is_stored_and_manifested(tmp_path, monkeypatch):
    provider = SessionArchiveProvider()
    provider.initialize("summary-session", hermes_home=str(tmp_path))
    provider._config = {"llm_summary": {"enabled": True, "max_tokens": 200}}

    def fake_call_llm(**kwargs):
        assert kwargs["task"] == "session_archive_summary"
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="Phase: routing setup\nClaims/Evidence: curl reached SJTU",
                        reasoning=None,
                    )
                )
            ]
        )

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)

    manifest = provider.on_pre_compress([
        {"role": "user", "content": "Configure SJTU VPN routing."},
        {"role": "assistant", "content": "curl reached SJTU."},
    ])

    assert "Phase: routing setup" in manifest
    result = provider.search("routing setup")
    assert result["results"][0]["summary"].startswith("Phase: routing setup")
    expanded = provider.expand(result["results"][0]["chunk_id"])
    assert "curl reached SJTU" in expanded["summary"]


def test_secondary_index_is_created_after_threshold(tmp_path):
    provider = SessionArchiveProvider()
    provider.initialize("secondary-session", hermes_home=str(tmp_path))
    provider.secondary_index_chunk_threshold = 2
    provider.secondary_index_group_size = 2
    provider.max_messages_per_chunk = 1

    provider.on_pre_compress([
        {"role": "user", "content": "stage one"},
        {"role": "assistant", "content": "stage two"},
        {"role": "user", "content": "stage three"},
    ])

    secondary = provider._read_secondary_index()
    assert secondary
    assert secondary[0]["group_id"] == "recap-1"
    assert len(secondary[0]["chunk_ids"]) == 2


def _summary_stub(calls, text="RECAP"):
    """A call_llm stand-in that counts invocations."""
    def fake_call_llm(**_kwargs):
        calls["n"] += 1
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=text, reasoning=None)
                )
            ]
        )
    return fake_call_llm


def test_unchanged_chunks_are_not_resummarized(tmp_path, monkeypatch):
    """Compression re-archives the whole transcript on every pass, and the
    chunk id is a hash of the chunk's content. Re-summarizing an identical chunk
    cost one LLM call per chunk per pass (measured: a byte-identical second pass
    still fired the summarizer)."""
    provider = SessionArchiveProvider()
    provider.initialize("idempotent-session", hermes_home=str(tmp_path))
    provider._config = {"llm_summary": {"enabled": True, "max_tokens": 200}}
    calls = {"n": 0}
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _summary_stub(calls))

    # Two messages past max_chars_per_chunk => two chunks.
    messages = [
        {"role": "user", "content": "a" * 40_000},
        {"role": "user", "content": "b" * 40_000},
    ]

    first = provider.on_pre_compress(messages)
    calls_after_first = calls["n"]
    second = provider.on_pre_compress(messages)

    assert calls_after_first >= 1, "the first pass must summarize"
    assert calls["n"] == calls_after_first, "identical input must not re-summarize"
    assert "chk-" in first and "chk-" in second
    assert provider.search("a" * 20)["results"]


def test_summarization_is_bounded_per_pass(tmp_path, monkeypatch):
    """A pass must spend a bounded number of LLM calls.

    The archive used to summarise every chunk of the transcript inside the
    compression pass, one blocking call at a time. On a 700-message session that
    blew the host's inactivity budget (compression.context_timeout_seconds,
    default 120s), compression was abandoned as "made no progress", and the
    context stayed over the provider's token limit. Chunks are always written;
    only the summaries are rationed.
    """
    provider = SessionArchiveProvider()
    provider.initialize("bounded-session", hermes_home=str(tmp_path))
    provider._config = {"llm_summary": {"enabled": True, "max_tokens": 200}}
    provider.max_messages_per_chunk = 1
    provider.max_llm_summaries_per_pass = 4
    calls = {"n": 0}
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _summary_stub(calls))

    manifest = provider.on_pre_compress(
        [{"role": "user", "content": f"msg {i}"} for i in range(40)]
    )

    assert calls["n"] == 4, "one summary per allowance, no more"
    chunks = provider._read_index()
    assert len(chunks) == 40, "every chunk is still archived"
    assert "chunk" in manifest or "chk-" in manifest
    assert provider.search("msg 3")["results"]


def test_summarization_prefers_the_newest_chunks(tmp_path, monkeypatch):
    provider = SessionArchiveProvider()
    provider.initialize("recency-session", hermes_home=str(tmp_path))
    provider._config = {"llm_summary": {"enabled": True, "max_tokens": 200}}
    provider.max_messages_per_chunk = 1
    provider.max_llm_summaries_per_pass = 2
    calls = {"n": 0}

    def fake_call_llm(**kwargs):
        calls["n"] += 1
        text = kwargs["messages"][-1]["content"]
        label = "OLD" if "msg 0" in text else "NEW"
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=f"{label}-summary", reasoning=None)
                )
            ]
        )

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)

    provider.on_pre_compress(
        [{"role": "user", "content": f"msg {i}"} for i in range(6)]
    )

    summaries = {item["summary"] for item in provider._read_index()}
    assert calls["n"] == 2
    assert "NEW-summary" in summaries
    assert "OLD-summary" not in summaries


def test_zero_budget_writes_chunks_without_any_llm_call(tmp_path, monkeypatch):
    provider = SessionArchiveProvider()
    provider.initialize("zero-budget", hermes_home=str(tmp_path))
    provider._config = {"llm_summary": {"enabled": True, "max_tokens": 200}}
    provider.max_llm_summaries_per_pass = 0
    calls = {"n": 0}
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _summary_stub(calls))

    provider.on_pre_compress([{"role": "user", "content": "only one"}])

    assert calls["n"] == 0
    assert provider.search("only one")["results"]


def test_wall_clock_budget_falls_back_to_deterministic_recap(tmp_path, monkeypatch):
    """The deadline bounds a slow provider even when the count allows more."""
    provider = SessionArchiveProvider()
    provider.initialize("deadline-session", hermes_home=str(tmp_path))
    provider._config = {"llm_summary": {"enabled": True, "max_tokens": 200}}
    provider.max_messages_per_chunk = 1
    provider.max_llm_summaries_per_pass = 10
    provider.llm_summary_budget_seconds = 0.0
    calls = {"n": 0}
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _summary_stub(calls))

    provider.on_pre_compress([{"role": "user", "content": f"m{i}"} for i in range(5)])

    assert calls["n"] == 0
    assert all(item["summary"] for item in provider._read_index())


def test_secondary_recaps_reuse_unchanged_groups(tmp_path, monkeypatch):
    provider = SessionArchiveProvider()
    provider.initialize("recap-session", hermes_home=str(tmp_path))
    provider._config = {"llm_summary": {"enabled": True, "max_tokens": 200}}
    provider.max_messages_per_chunk = 1
    calls = {"n": 0}
    monkeypatch.setattr("agent.auxiliary_client.call_llm", _summary_stub(calls))

    provider.on_pre_compress(
        [{"role": "user", "content": f"stage {i}"} for i in range(2)]
    )

    # Manual pass so the recount starts from a known state.
    provider.secondary_index_chunk_threshold = 2
    provider.secondary_index_group_size = 1
    calls["n"] = 0
    provider._maybe_write_secondary_index()
    first_pass = calls["n"]
    provider._maybe_write_secondary_index()
    second_pass = calls["n"] - first_pass

    assert first_pass == 2, "one recap per group on a cold index"
    assert second_pass == 0, "unchanged inputs must not recompute recap groups"
    groups = provider._read_secondary_index()
    assert all(group.get("fingerprint") for group in groups)

    # Only the affected group is recomputed when a member changes.
    index = provider._read_index()
    provider._index_path().write_text(
        json.dumps([{**index[0], "summary": "CHANGED"}, index[1]]), encoding="utf-8"
    )
    calls["n"] = 0
    provider._maybe_write_secondary_index()
    assert calls["n"] == 1, "granular invalidation: only the changed group"
