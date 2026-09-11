from plugins.context_engine import load_context_engine
from plugins.context_engine.long_horizon import LongHorizonContextEngine


def _messages(count=28):
    items = [{"role": "system", "content": "system prompt"}]
    for idx in range(count):
        role = "user" if idx % 2 == 0 else "assistant"
        items.append({"role": role, "content": f"message {idx} unique-old-detail"})
    return items


def test_load_context_engine_by_name():
    engine = load_context_engine("long_horizon")
    assert engine is not None
    assert engine.name == "long_horizon"


def test_should_compress_uses_threshold():
    engine = LongHorizonContextEngine()
    engine.update_model(model="test", context_length=1000)
    assert engine.should_compress(749) is False
    assert engine.should_compress(750) is True


def test_compress_keeps_manifest_and_recent_tail():
    engine = LongHorizonContextEngine()
    engine.update_model(model="test", context_length=1000)
    engine.on_session_start("sid-1", platform="cli", model="test")

    compacted = engine.compress(
        _messages(),
        current_tokens=900,
        memory_context="[SESSION ARCHIVE CHECKPOINT]\n- chk-0-10-abc preview=old tool output",
    )

    joined = "\n".join(str(m.get("content") or "") for m in compacted)
    assert len(compacted) < len(_messages())
    assert "session_archive_search" in joined
    assert "chk-0-10-abc" in joined
    assert "message 27" in joined
    assert "message 5 unique-old-detail" not in joined


def test_compress_includes_task_board_handoff(tmp_path, monkeypatch):
    from agent.longtask_board import create_board, update_node

    monkeypatch.chdir(tmp_path)
    session_id = "sid-board"
    root = tmp_path / ".hermes" / "tasks"
    create_board(
        root,
        session_id,
        "Ship long-horizon compression",
        [
            {"node_id": "N1", "goal": "Archive old context"},
            {"node_id": "N2", "goal": "Add task board handoff", "dependencies": ["N1"]},
            {"node_id": "N3", "goal": "Verify behavior", "dependencies": ["N2"]},
        ],
    )
    update_node(
        root,
        session_id,
        "N1",
        status="done",
        claims=[{"claim": "Archive provider stores chunks"}],
        verification={
            "verdict": "accepted",
            "summary_for_parent": "Chunk archive test passed.",
        },
    )

    engine = LongHorizonContextEngine()
    engine.update_model(model="test", context_length=1000)
    engine.on_session_start(session_id, platform="cli", model="test")

    compacted = engine.compress(
        _messages(),
        current_tokens=900,
        memory_context="[SESSION ARCHIVE CHECKPOINT]\n- chk-0-10-abc summary=old",
    )

    joined = "\n".join(str(m.get("content") or "") for m in compacted)
    assert "Task board state:" in joined
    assert "objective: Ship long-horizon compression" in joined
    assert "ready_frontier:" in joined
    assert "N2" in joined
    assert "recent_terminal:" in joined
    assert "verification=accepted" in joined
    assert "Archive provider stores chunks" in joined
