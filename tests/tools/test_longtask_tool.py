import json

from tools.longtask_tool import (
    longtask_attach_report_handler,
    longtask_create_handler,
    longtask_next_handler,
    longtask_update_node_handler,
    longtask_verify_node_handler,
)


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
