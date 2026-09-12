import json
from pathlib import Path

from tools.longtask_tool import (
    _board_handler,
    longtask_add_node_handler,
    longtask_attach_report_handler,
    longtask_cancel_node_handler,
    longtask_create_handler,
    longtask_next_handler,
    longtask_update_node_handler,
    longtask_verify_node_handler,
)

LONGTASK_TOOL_NAMES = {
    "longtask_create",
    "longtask_add_node",
    "longtask_cancel_node",
    "longtask_read",
    "longtask_next",
    "longtask_update_node",
    "longtask_attach_report",
    "longtask_verify_node",
}


def _loads(result):
    return json.loads(result)


def test_longtask_tool_minimal_flow(tmp_path):
    """End-to-end through the tool surface: a report lands, but only an explicit
    `resolved` — after the verdict — unlocks the dependent item."""
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
        longtask_update_node_handler(
            {**common, "node_id": "N1", "resolution": "in_progress"}
        )
    )
    assert updated["node"]["resolution"] == "in_progress"

    attached = _loads(
        longtask_attach_report_handler(
            {
                **common,
                "node_id": "N1",
                "report": {
                    "status": "success",
                    "claims": [{"claim": "Researched", "evidence": []}],
                },
            }
        )
    )
    assert attached["node"]["execution"] == "reported"
    assert attached["node"]["resolution"] == "in_progress"

    verify = _loads(longtask_verify_node_handler({**common, "node_id": "N1"}))
    assert verify["verification"]["verdict"] == "rejected"

    # A rejected claim does not unlock downstream work: the item is still open.
    after_rejection = _loads(longtask_next_handler(common))
    assert after_rejection["ready"] == []
    assert {n["node_id"]: n for n in after_rejection["blocked"]}["N2"]["blocked_by"] == [
        "N1"
    ]

    resolved = _loads(
        longtask_update_node_handler({**common, "node_id": "N1", "resolution": "resolved"})
    )
    assert resolved["node"]["resolution"] == "resolved"

    second = _loads(longtask_next_handler(common))
    assert [node["node_id"] for node in second["ready"]] == ["N2"]


def test_verifier_review_cap_comes_from_config(tmp_path, monkeypatch):
    """The per-claim LLM fan-out is bounded by longtask.verifier_max_claim_reviews.

    Without the cap a 20-claim report becomes 20 unbounded model calls. Claims
    past the cap stay `unverified` on the stored verification, and an unverified
    load-bearing claim must not read as an accepted verdict.
    """
    calls = []

    class _Msg:
        content = (
            '{"contested": false, "disconfirming_evidence": [], "required_repair": ""}'
        )

    class _Choice:
        message = _Msg()

    class _Response:
        choices = [_Choice()]

    def fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _Response()

    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "longtask": {
                "verifier": {"enabled": True},
                "verifier_max_claim_reviews": 1,
            }
        },
    )

    common = {"root": str(tmp_path), "session_id": "s"}
    _loads(
        longtask_create_handler(
            {**common, "objective": "obj", "nodes": [{"node_id": "N1", "goal": "g"}]}
        )
    )
    _loads(longtask_update_node_handler({**common, "node_id": "N1", "resolution": "in_progress"}))
    _loads(
        longtask_attach_report_handler(
            {
                **common,
                "node_id": "N1",
                "report": {
                    "status": "success",
                    "claims": [
                        {"claim": "First", "evidence": [{"kind": "observation", "ref": "obs-1"}]},
                        {"claim": "Second", "evidence": [{"kind": "observation", "ref": "obs-2"}]},
                    ],
                },
            }
        )
    )

    verified = _loads(longtask_verify_node_handler({**common, "node_id": "N1"}))
    verification = verified["verification"]

    assert len(calls) == 1
    assert verification["llm_verification"]["max_claim_reviews"] == 1
    assert [c["claim"] for c in verification["unverified_claims"]] == ["Second"]
    assert [c["claim"] for c in verification["accepted_claims"]] == ["First"]
    assert verification["verdict"] != "accepted"


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


class TestMutableBoardToolSurface:
    """Mid-run replanning through the tools: append, cancel, rewire."""

    def test_add_then_cancel_then_rewire(self, tmp_path):
        common = {"root": str(tmp_path), "session_id": "s"}
        _loads(
            longtask_create_handler(
                {**common, "objective": "Ship", "nodes": [{"node_id": "N1", "goal": "Research"}]}
            )
        )

        added = _loads(
            longtask_add_node_handler(
                {
                    **common,
                    "nodes": [
                        {"node_id": "N2", "goal": "Follow-up", "dependencies": ["N1"]}
                    ],
                }
            )
        )
        assert added["added"] == ["N2"]

        cancelled = _loads(
            longtask_cancel_node_handler(
                {**common, "node_id": "N1", "reason": "superseded"}
            )
        )
        assert cancelled["node"]["resolution"] == "cancelled"
        assert cancelled["dependents_to_review"] == ["N2"]

        rewired = _loads(
            longtask_update_node_handler({**common, "node_id": "N2", "dependencies": []})
        )
        assert rewired["node"]["dependencies"] == []

        assert [n["node_id"] for n in _loads(longtask_next_handler(common))["ready"]] == [
            "N2"
        ]

    def test_revising_a_goal_through_the_tool(self, tmp_path):
        common = {"root": str(tmp_path), "session_id": "s"}
        _loads(
            longtask_create_handler(
                {**common, "objective": "Ship", "nodes": [{"node_id": "N1", "goal": "Old"}]}
            )
        )

        out = _loads(longtask_update_node_handler({**common, "node_id": "N1", "goal": "New"}))

        assert out["node"]["goal"] == "New"

    def test_validation_errors_surface_as_tool_errors(self, tmp_path):
        common = {"root": str(tmp_path), "session_id": "s"}
        _loads(
            longtask_create_handler(
                {**common, "objective": "Ship", "nodes": [{"node_id": "N1", "goal": "g"}]}
            )
        )

        clash = _loads(
            longtask_add_node_handler({**common, "nodes": [{"node_id": "N1", "goal": "x"}]})
        )

        assert "error" in clash


class TestBoardSlashCommand:
    def test_registered_with_a_non_colliding_alias(self):
        from hermes_cli.commands import resolve_command

        cmd = resolve_command("board")
        assert cmd is not None and cmd.name == "board"
        assert resolve_command("longtask").name == "board"
        # /tasks belongs to /agents — this alias must not have shadowed it.
        assert resolve_command("tasks").name == "agents"


class TestArtifactIndexSurface:
    """Artifact registration + delivery manifest through the existing tools.

    The index lives under the profile home (HERMES_HOME), NOT the workspace, and
    the verifier tool consumes it: a claim citing a scratch path is refused while
    a final deliverable is accepted and listed in the manifest it returns.
    """

    def _env(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("TERMINAL_CWD", str(ws))
        return home, ws

    def _common(self, tmp_path, sid):
        return {"root": str(tmp_path / "board"), "session_id": sid}

    def _create(self, tmp_path, sid):
        common = self._common(tmp_path, sid)
        _loads(
            longtask_create_handler(
                {
                    **common,
                    "objective": "Ship the deliverable",
                    "nodes": [{"node_id": "N1", "goal": "Produce the report"}],
                }
            )
        )
        return common

    def _attach(self, common, ref):
        _loads(
            longtask_attach_report_handler(
                {
                    **common,
                    "node_id": "N1",
                    "report": {
                        "status": "success",
                        "claims": [
                            {
                                "claim": "The report was produced",
                                "evidence": [
                                    {"kind": "file", "ref": ref, "quote": "delivered"}
                                ],
                            }
                        ],
                    },
                }
            )
        )

    def test_update_node_registers_and_verify_reports_the_manifest(
        self, tmp_path, monkeypatch
    ):
        from agent.longtask_board import artifact_index_path

        home, ws = self._env(tmp_path, monkeypatch)
        (ws / "report.md").write_text("delivered\n", encoding="utf-8")
        common = self._create(tmp_path, "sess-final")

        updated = _loads(
            longtask_update_node_handler(
                {
                    **common,
                    "node_id": "N1",
                    "artifacts": [{"path": "report.md", "kind": "final"}],
                }
            )
        )

        assert updated["artifacts"]["counts"]["final"] == 1
        assert updated["artifacts"]["registered"][0]["producing_node_id"] == "N1"
        # Stored under the profile home, not the workspace or the board root.
        assert str(home) in updated["artifacts"]["index_path"]
        assert artifact_index_path("sess-final").exists()

        self._attach(common, "report.md")
        verified = _loads(longtask_verify_node_handler({**common, "node_id": "N1"}))

        assert verified["verification"]["verdict"] == "accepted"
        assert verified["deliverables"]["counts"]["final"] == 1
        assert Path(verified["manifest_path"]).exists()
        assert Path(verified["manifest_path"]).parent == artifact_index_path(
            "sess-final"
        ).parent

    def test_scratch_artifact_is_refused_as_a_deliverable(
        self, tmp_path, monkeypatch
    ):
        _home, ws = self._env(tmp_path, monkeypatch)
        (ws / "tmp-work.txt").write_text("delivered\n", encoding="utf-8")
        common = self._create(tmp_path, "sess-scratch")

        _loads(
            longtask_update_node_handler(
                {
                    **common,
                    "node_id": "N1",
                    "artifacts": [{"path": "tmp-work.txt", "kind": "scratch"}],
                }
            )
        )
        self._attach(common, "tmp-work.txt")

        verified = _loads(longtask_verify_node_handler({**common, "node_id": "N1"}))

        assert verified["verification"]["verdict"] == "rejected"
        assert verified["deliverables"]["counts"]["scratch"] == 1
        assert verified["deliverables"]["final"] == []

    def test_read_can_return_the_delivery_manifest(self, tmp_path, monkeypatch):
        from tools.longtask_tool import longtask_read_handler

        _home, ws = self._env(tmp_path, monkeypatch)
        (ws / "ship.txt").write_text("delivered\n", encoding="utf-8")
        common = self._create(tmp_path, "sess-read")
        _loads(
            longtask_update_node_handler(
                {
                    **common,
                    "node_id": "N1",
                    "artifacts": [{"path": "ship.txt", "kind": "final"}],
                }
            )
        )

        without = _loads(longtask_read_handler(common))
        assert "deliverables" not in without

        with_manifest = _loads(longtask_read_handler({**common, "include_artifacts": True}))
        assert with_manifest["deliverables"]["counts"]["final"] == 1
