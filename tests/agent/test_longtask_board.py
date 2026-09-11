import json

import pytest

from agent.longtask_board import (
    LongtaskBoardError,
    attach_report,
    compute_ready_nodes,
    create_board,
    load_board,
    next_ready_nodes,
    update_node,
)


def _nodes():
    return [
        {"node_id": "N1", "goal": "Research constraints"},
        {"node_id": "N2", "goal": "Implement feature", "dependencies": ["N1"]},
        {"node_id": "N3", "goal": "Verify behavior", "dependencies": ["N2"]},
    ]


def test_create_board_computes_initial_ready(tmp_path):
    board = create_board(tmp_path, "session-1", "Ship longtask", _nodes())

    assert board["objective"] == "Ship longtask"
    assert board["global_state"]["next_ready"] == ["N1"]
    assert (tmp_path / "session-1" / "board.json").exists()


def test_ready_nodes_follow_dependency_order(tmp_path):
    create_board(tmp_path, "s", "obj", _nodes())

    first = next_ready_nodes(tmp_path, "s")
    assert [node["node_id"] for node in first["ready"]] == ["N1"]

    update_node(tmp_path, "s", "N1", status="done")
    second = next_ready_nodes(tmp_path, "s")
    assert [node["node_id"] for node in second["ready"]] == ["N2"]

    update_node(tmp_path, "s", "N2", status="done")
    third = next_ready_nodes(tmp_path, "s")
    assert [node["node_id"] for node in third["ready"]] == ["N3"]


def test_rejects_unknown_dependency(tmp_path):
    with pytest.raises(LongtaskBoardError, match="unknown node"):
        create_board(
            tmp_path,
            "s",
            "obj",
            [{"node_id": "N1", "goal": "Bad dep", "dependencies": ["missing"]}],
        )


def test_rejects_dependency_cycle(tmp_path):
    with pytest.raises(LongtaskBoardError, match="cycle"):
        create_board(
            tmp_path,
            "s",
            "obj",
            [
                {"node_id": "N1", "goal": "one", "dependencies": ["N2"]},
                {"node_id": "N2", "goal": "two", "dependencies": ["N1"]},
            ],
        )


def test_rejects_running_before_dependencies_done(tmp_path):
    create_board(tmp_path, "s", "obj", _nodes())

    with pytest.raises(LongtaskBoardError, match="dependencies"):
        update_node(tmp_path, "s", "N2", status="running")


def test_attach_report_persists_full_report_and_updates_node(tmp_path):
    create_board(tmp_path, "s", "obj", _nodes())
    report = {
        "status": "done",
        "claims": [
            {
                "claim": "Constraints identified",
                "evidence": [{"kind": "file", "ref": "docs/plans/x.md"}],
            }
        ],
        "open_questions": [],
    }

    result = attach_report(tmp_path, "s", "N1", report)

    assert result["node"]["status"] == "done"
    assert result["node"]["claims"] == report["claims"]
    saved = load_board(tmp_path, "s")
    report_path = tmp_path / "s" / saved["nodes"][0]["report_path"]
    assert json.loads(report_path.read_text(encoding="utf-8"))["claims"] == report["claims"]


def test_compute_ready_nodes_is_pure_for_loaded_board(tmp_path):
    board = create_board(tmp_path, "s", "obj", _nodes())
    ready = compute_ready_nodes(board)
    assert [node["node_id"] for node in ready] == ["N1"]
