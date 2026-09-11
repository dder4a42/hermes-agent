import json

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
