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
