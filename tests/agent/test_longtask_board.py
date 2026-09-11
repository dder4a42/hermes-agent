import json
import threading

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


class TestReportStatusVocabulary:
    """Children report success|partial|failed|timeout; the board tracks
    lifecycle statuses. Handing one straight to the other used to raise
    "Invalid status: success" on the documented happy path."""

    def test_success_maps_to_done(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())
        update_node(tmp_path, "s", "N1", status="running")

        result = attach_report(tmp_path, "s", "N1", {"status": "success"})

        assert result["node"]["status"] == "done"
        assert result["node"]["report_status"] == "done"

    def test_partial_maps_to_a_non_terminal_status(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())
        update_node(tmp_path, "s", "N1", status="running")

        result = attach_report(tmp_path, "s", "N1", {"status": "partial"})

        assert result["node"]["status"] == "blocked"

    def test_unknown_status_names_both_vocabularies(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())
        update_node(tmp_path, "s", "N1", status="running")

        with pytest.raises(LongtaskBoardError) as excinfo:
            attach_report(tmp_path, "s", "N1", {"status": "gibberish"})

        message = str(excinfo.value)
        assert "board" in message.lower() and "report" in message.lower()


class TestConcurrentWrites:
    def test_parallel_updates_all_land(self, tmp_path):
        """The runtime runs a turn's independent tool calls on worker threads.
        Before the board lock, load -> mutate -> save silently dropped updates
        (measured: 6 concurrent updates left 2 applied and zero errors)."""
        create_board(
            tmp_path, "s", "obj", [{"node_id": f"N{i}", "goal": "g"} for i in range(1, 7)]
        )

        def worker(index):
            update_node(tmp_path, "s", f"N{index}", status="running")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 7)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        board = load_board(tmp_path, "s")
        running = [n["node_id"] for n in board["nodes"] if n["status"] == "running"]
        assert running == [f"N{i}" for i in range(1, 7)]


class TestVerificationGate:
    """Optional host-side enforcement of the skill's "no downstream work before
    a verification result" rule. Off unless
    longtask.require_verification_before_unlock is set."""

    def _enable_gate(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"longtask": {"require_verification_before_unlock": True}},
        )

    def test_off_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly", lambda: {"longtask": {}}
        )
        create_board(tmp_path, "s", "obj", _nodes())

        assert update_node(tmp_path, "s", "N1", status="done")["node"]["status"] == "done"

    def test_blocks_done_without_a_verdict(self, tmp_path, monkeypatch):
        self._enable_gate(monkeypatch)
        create_board(tmp_path, "s", "obj", _nodes())
        update_node(tmp_path, "s", "N1", status="running")

        with pytest.raises(LongtaskBoardError, match="verification"):
            update_node(tmp_path, "s", "N1", status="done")

    def test_refused_attach_still_persists_the_report(self, tmp_path, monkeypatch):
        """A child's self-report is a claim, not a verdict: keep the evidence,
        refuse only the terminal transition."""
        self._enable_gate(monkeypatch)
        create_board(tmp_path, "s", "obj", _nodes())
        update_node(tmp_path, "s", "N1", status="running")
        report = {"status": "success", "claims": [{"claim": "c", "evidence": []}]}

        with pytest.raises(LongtaskBoardError, match="verification"):
            attach_report(tmp_path, "s", "N1", report)

        node = load_board(tmp_path, "s")["nodes"][0]
        assert node["node_id"] == "N1"
        assert node["report_path"], "the report must survive the refusal"
        assert node["status"] == "running"
        assert node["report_status"] == "done"

    def test_verify_then_done_is_allowed(self, tmp_path, monkeypatch):
        self._enable_gate(monkeypatch)
        create_board(tmp_path, "s", "obj", _nodes())
        update_node(tmp_path, "s", "N1", status="running")
        update_node(tmp_path, "s", "N1", verification={"verdict": "accepted"})

        assert update_node(tmp_path, "s", "N1", status="done")["node"]["status"] == "done"
        ready = next_ready_nodes(tmp_path, "s")
        assert [n["node_id"] for n in ready["ready"]] == ["N2"]
