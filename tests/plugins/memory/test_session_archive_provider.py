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
