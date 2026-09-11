import json

from tools.longtask_tool import (
    _board_handler,
    longtask_attach_report_handler,
    longtask_create_handler,
    longtask_next_handler,
    longtask_update_node_handler,
    longtask_verify_node_handler,
)

LONGTASK_TOOL_NAMES = {
    "longtask_create",
    "longtask_read",
    "longtask_next",
    "longtask_update_node",
    "longtask_attach_report",
    "longtask_verify_node",
}


def _loads(result):
    return json.loads(result)


def test_longtask_tool_minimal_flow(tmp_path):
    common = {"root": str(tmp_path), "session_id": "s"}

    created = _loads(
        longtask_create_handler(
            {
                **common,
                "objective": "Ship",
                "nodes": [
                    {"node_id": "N1", "goal": "Research"},
                    {"node_id": "N2", "goal": "Implement", "dependencies": ["N1"]},
                ],
            }
        )
    )
    assert created["status"] == "created"
    assert created["next_ready"] == ["N1"]

    first = _loads(longtask_next_handler(common))
    assert [node["node_id"] for node in first["ready"]] == ["N1"]

    updated = _loads(
        longtask_update_node_handler({**common, "node_id": "N1", "status": "running"})
    )
    assert updated["node"]["status"] == "running"

    attached = _loads(
        longtask_attach_report_handler(
            {
                **common,
                "node_id": "N1",
                "report": {
                    "status": "done",
                    "claims": [{"claim": "Researched", "evidence": []}],
                },
            }
        )
    )
    assert attached["node"]["status"] == "done"

    verify = _loads(longtask_verify_node_handler({**common, "node_id": "N1"}))
    assert verify["verification"]["verdict"] == "rejected"

    second = _loads(longtask_next_handler(common))
    assert [node["node_id"] for node in second["ready"]] == ["N2"]


class TestDelegatedChildIsolation:
    """Division of labour: the main agent plans and owns the board; a subagent
    executes one bounded node and reports back. A child used to receive all six
    board tools while resolving a DIFFERENT board (its own generated
    session_id), so a call either errored or silently created a shadow board."""

    def test_runtime_refusal_inside_a_child_context(self, tmp_path):
        from agent.delegation_context import delegated_child_context

        common = {"root": str(tmp_path), "session_id": "s"}
        guarded = {
            "create": _board_handler(longtask_create_handler),
            "next": _board_handler(longtask_next_handler),
            "attach": _board_handler(longtask_attach_report_handler),
        }
        with delegated_child_context("child-session"):
            for name, handler in guarded.items():
                result = handler({**common, "objective": "x", "nodes": []})
                assert "Refused" in result, f"{name} was not refused for a child"

    def test_parent_context_is_unaffected(self, tmp_path):
        from agent.delegation_context import delegated_child_context

        common = {"root": str(tmp_path), "session_id": "s"}
        handler = _board_handler(longtask_create_handler)

        with delegated_child_context("child-session"):
            assert "Refused" in handler({**common, "objective": "x", "nodes": []})

        outside = json.loads(
            handler(
                {
                    **common,
                    "objective": "Ship",
                    "nodes": [{"node_id": "N1", "goal": "Research"}],
                }
            )
        )
        assert outside["status"] == "created"

    def test_children_never_receive_board_schemas(self):
        """Both layers matter: the toolset is stripped, AND the names are passed
        as deny toolsets so they are subtracted after composite expansion (the
        `coding` posture toolset carries the board tools inline)."""
        from model_tools import get_tool_definitions
        from tools.delegate_tool import (
            _blocked_toolsets_for_role,
            _strip_blocked_tools,
        )

        deny = _blocked_toolsets_for_role("leaf")
        assert "longtask" in deny

        for parent_toolsets in (["hermes-cli"], ["coding"]):
            child_toolsets = _strip_blocked_tools(list(parent_toolsets))
            assert "longtask" not in child_toolsets
            names = {
                d["function"]["name"]
                for d in get_tool_definitions(
                    enabled_toolsets=child_toolsets,
                    disabled_toolsets=deny,
                    quiet_mode=True,
                )
            }
            assert not (names & LONGTASK_TOOL_NAMES), (
                f"board tools leaked into a child of {parent_toolsets}: "
                f"{sorted(names & LONGTASK_TOOL_NAMES)}"
            )


class TestRuntimeContextResolution:
    """The runtime dispatch (`model_tools.handle_function_call` ->
    `registry.dispatch(..., task_id=, session_id=, user_task=)`) passes
    ``session_id`` but never ``parent_agent`` — only the plugin path injects a
    parent agent. Keying the board off ``parent_agent`` alone silently wrote
    every session's board into one shared "default" directory, while the
    compression engine looked the board up under the real session id and never
    found it."""

    def _dispatch(self, args, **kwargs):
        import tools.longtask_tool  # noqa: F401  (registers the tools)
        from tools.registry import registry

        result = registry.dispatch("longtask_create", args, **kwargs)
        return result if isinstance(result, dict) else json.loads(result)

    def test_dispatch_session_id_replaces_the_default_fallback(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        result = self._dispatch(
            {"objective": "obj", "nodes": [{"node_id": "N1", "goal": "g"}]},
            session_id="sid-runtime",
        )

        assert result["session_id"] == "sid-runtime"
        assert (tmp_path / ".hermes" / "tasks" / "sid-runtime" / "board.json").exists()
        assert not (tmp_path / ".hermes" / "tasks" / "default").exists()

    def test_explicit_argument_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        result = self._dispatch(
            {
                "objective": "obj",
                "nodes": [{"node_id": "N1", "goal": "g"}],
                "session_id": "sid-explicit",
            },
            session_id="sid-runtime",
        )

        assert result["session_id"] == "sid-explicit"

    def test_terminal_cwd_decides_the_board_root(self, tmp_path, monkeypatch):
        """The gateway bridges terminal.cwd into TERMINAL_CWD; the writer and the
        compression engine must agree on that root or the board drops out of the
        handoff."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("TERMINAL_CWD", str(workspace))

        self._dispatch(
            {"objective": "obj", "nodes": [{"node_id": "N1", "goal": "g"}]},
            session_id="sid-gateway",
        )

        assert (workspace / ".hermes" / "tasks" / "sid-gateway" / "board.json").exists()
        assert not (elsewhere / ".hermes").exists()

    def test_compression_engine_sees_a_dispatch_written_board(
        self, tmp_path, monkeypatch
    ):
        """End-to-end contract: whatever the writer resolves, the reader must
        resolve too — otherwise the long-horizon handoff renders no board."""
        from plugins.context_engine.long_horizon import LongHorizonContextEngine

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.chdir(workspace)

        session_id = "sid-e2e"
        self._dispatch(
            {
                "objective": "Gateway objective",
                "nodes": [{"node_id": "N1", "goal": "Drain the queue"}],
            },
            session_id=session_id,
        )

        engine = LongHorizonContextEngine()
        engine.update_model(model="test", context_length=1000)
        engine.on_session_start(session_id, platform="gateway", model="test")

        handoff = engine._handoff_message(
            original_count=9,
            retained_count=2,
            current_tokens=1,
            focus_topic=None,
            memory_context="",
        )

        assert "Task board state:" in handoff["content"]
        assert "Drain the queue" in handoff["content"]
