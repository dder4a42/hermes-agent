import json
import threading

import pytest

from agent.longtask_board import (
    LongtaskBoardError,
    add_nodes,
    attach_report,
    cancel_node,
    compute_ready_nodes,
    create_board,
    load_board,
    next_ready_nodes,
    render_board_summary,
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


def test_new_items_start_open_and_unexecuted(tmp_path):
    """The two axes start independently: nothing has run, nothing is decided."""
    board = create_board(tmp_path, "s", "obj", _nodes())

    assert board["nodes"][0]["resolution"] == "open"
    assert board["nodes"][0]["execution"] == "none"


def test_ready_nodes_follow_dependency_order(tmp_path):
    create_board(tmp_path, "s", "obj", _nodes())

    first = next_ready_nodes(tmp_path, "s")
    assert [node["node_id"] for node in first["ready"]] == ["N1"]

    update_node(tmp_path, "s", "N1", resolution="resolved")
    second = next_ready_nodes(tmp_path, "s")
    assert [node["node_id"] for node in second["ready"]] == ["N2"]

    update_node(tmp_path, "s", "N2", resolution="resolved")
    third = next_ready_nodes(tmp_path, "s")
    assert [node["node_id"] for node in third["ready"]] == ["N3"]


def test_blocked_items_name_what_they_wait_on(tmp_path):
    create_board(tmp_path, "s", "obj", _nodes())

    blocked = {n["node_id"]: n for n in next_ready_nodes(tmp_path, "s")["blocked"]}

    assert blocked["N2"]["blocked_by"] == ["N1"]


def test_blocked_reason_parks_an_item_out_of_the_frontier(tmp_path):
    create_board(tmp_path, "s", "obj", _nodes())

    update_node(tmp_path, "s", "N1", blocked_reason="need the user's scope decision")
    parked = next_ready_nodes(tmp_path, "s")
    assert parked["ready"] == []
    assert {n["node_id"]: n for n in parked["blocked"]}["N1"]["blocked_by"] == [
        "need the user's scope decision"
    ]

    update_node(tmp_path, "s", "N1", blocked_reason="")
    assert [n["node_id"] for n in next_ready_nodes(tmp_path, "s")["ready"]] == ["N1"]


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


def test_rejects_resolving_before_dependencies_are_resolved(tmp_path):
    create_board(tmp_path, "s", "obj", _nodes())

    with pytest.raises(LongtaskBoardError, match="dependencies"):
        update_node(tmp_path, "s", "N2", resolution="resolved")


def test_rejects_unknown_resolution(tmp_path):
    create_board(tmp_path, "s", "obj", _nodes())

    with pytest.raises(LongtaskBoardError, match="Invalid resolution"):
        update_node(tmp_path, "s", "N1", resolution="nonsense")


def test_attach_report_persists_the_report_and_marks_execution(tmp_path):
    create_board(tmp_path, "s", "obj", _nodes())
    report = {
        "status": "success",
        "claims": [
            {
                "claim": "Constraints identified",
                "evidence": [{"kind": "file", "ref": "docs/plans/x.md"}],
            }
        ],
        "open_questions": [],
    }

    result = attach_report(tmp_path, "s", "N1", report)

    assert result["node"]["execution"] == "reported"
    assert result["node"]["resolution"] == "open"
    assert result["node"]["claims"] == report["claims"]
    saved = load_board(tmp_path, "s")
    report_path = tmp_path / "s" / saved["nodes"][0]["report_path"]
    assert json.loads(report_path.read_text(encoding="utf-8"))["claims"] == report["claims"]


def test_compute_ready_nodes_is_pure_for_loaded_board(tmp_path):
    board = create_board(tmp_path, "s", "obj", _nodes())
    ready = compute_ready_nodes(board)
    assert [node["node_id"] for node in ready] == ["N1"]


class TestLegacyBoardMigration:
    """Boards written before the execution/resolution split must keep loading."""

    def _write_legacy(self, tmp_path):
        legacy = {
            "task_id": "task-legacy",
            "objective": "old board",
            "nodes": [
                {"node_id": "N1", "goal": "done one", "status": "done"},
                {"node_id": "N2", "goal": "running one", "status": "running"},
                {
                    "node_id": "N3",
                    "goal": "queued behind N1",
                    "status": "pending",
                    "dependencies": ["N1"],
                },
            ],
            "global_state": {},
        }
        board_dir = tmp_path / "s"
        board_dir.mkdir(parents=True, exist_ok=True)
        (board_dir / "board.json").write_text(json.dumps(legacy), encoding="utf-8")

    def test_statuses_map_onto_the_two_axes(self, tmp_path):
        self._write_legacy(tmp_path)

        board = load_board(tmp_path, "s")
        axes = {n["node_id"]: (n["execution"], n["resolution"]) for n in board["nodes"]}

        assert axes["N1"] == ("none", "resolved")
        assert axes["N2"] == ("running", "in_progress")
        assert axes["N3"] == ("none", "open")

    def test_migrated_done_item_unlocks_its_dependent(self, tmp_path):
        self._write_legacy(tmp_path)

        ready = next_ready_nodes(tmp_path, "s")

        assert [n["node_id"] for n in ready["ready"]] == ["N3"]

    def test_legacy_done_without_a_report_is_not_marked_reported(self, tmp_path):
        self._write_legacy(tmp_path)
        board = load_board(tmp_path, "s")
        n1 = next(n for n in board["nodes"] if n["node_id"] == "N1")
        assert n1["execution"] == "none"


class TestMutableBoard:
    """The DAG is not frozen at create time.

    AgentOS §3.3.2: the board stays mutable during execution "so new
    subquestions can be registered as evidence changes the plan", and plan
    revisions are tool-mediated edits to it rather than a rebuild.
    """

    def test_add_node_appends_and_waits_for_existing_dependencies(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())

        result = add_nodes(
            tmp_path,
            "s",
            [{"node_id": "N4", "goal": "follow-up work", "dependencies": ["N1"]}],
        )

        assert result["added"] == ["N4"]
        assert result["next_ready"] == ["N1"]

        update_node(tmp_path, "s", "N1", resolution="resolved")
        ready = [n["node_id"] for n in next_ready_nodes(tmp_path, "s")["ready"]]
        assert ready == ["N2", "N4"]

    def test_add_node_rejects_a_duplicate_id(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())

        with pytest.raises(LongtaskBoardError, match="already exists"):
            add_nodes(tmp_path, "s", [{"node_id": "N1", "goal": "clash"}])

    def test_add_node_rejects_unknown_dependency(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())

        with pytest.raises(LongtaskBoardError, match="unknown node"):
            add_nodes(
                tmp_path,
                "s",
                [{"node_id": "N4", "goal": "g", "dependencies": ["NOPE"]}],
            )

    def test_add_node_rejects_a_cycle_inside_the_batch(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())

        with pytest.raises(LongtaskBoardError, match="cycle"):
            add_nodes(
                tmp_path,
                "s",
                [
                    {"node_id": "N4", "goal": "a", "dependencies": ["N5"]},
                    {"node_id": "N5", "goal": "b", "dependencies": ["N4"]},
                ],
            )

    def test_cancel_records_the_reason_and_reports_dependents(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())

        result = cancel_node(tmp_path, "s", "N1", reason="premise was wrong")

        assert result["node"]["resolution"] == "cancelled"
        assert "premise was wrong" in result["node"]["notes"]
        assert result["dependents_to_review"] == ["N2"]

    def test_dependent_of_a_cancelled_item_is_flagged_not_released(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())
        cancel_node(tmp_path, "s", "N1")

        frontier = next_ready_nodes(tmp_path, "s")

        assert frontier["ready"] == []
        entry = {n["node_id"]: n for n in frontier["blocked"]}["N2"]
        assert entry["blocked_by"] == ["N1"]
        assert entry["blocked_by_cancelled"] == ["N1"]

    def test_rewiring_releases_a_stranded_dependent(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())
        cancel_node(tmp_path, "s", "N1")

        update_node(tmp_path, "s", "N2", dependencies=[])

        assert [n["node_id"] for n in next_ready_nodes(tmp_path, "s")["ready"]] == ["N2"]

    def test_update_node_revises_goal_and_dependencies(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())

        result = update_node(
            tmp_path,
            "s",
            "N3",
            goal="Verify the revised behavior",
            dependencies=["N1"],
        )

        assert result["node"]["goal"] == "Verify the revised behavior"
        assert result["node"]["dependencies"] == ["N1"]

    def test_rewire_rejects_self_unknown_and_cycles(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())

        with pytest.raises(LongtaskBoardError, match="cannot depend on itself"):
            update_node(tmp_path, "s", "N1", dependencies=["N1"])
        with pytest.raises(LongtaskBoardError, match="unknown node"):
            update_node(tmp_path, "s", "N3", dependencies=["NOPE"])
        with pytest.raises(LongtaskBoardError, match="cycle"):
            update_node(tmp_path, "s", "N1", dependencies=["N3"])

    def test_revising_a_goal_must_not_empty_it(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())

        with pytest.raises(LongtaskBoardError, match="non-empty"):
            update_node(tmp_path, "s", "N1", goal="   ")


class TestBoardRendering:
    def test_summary_names_every_section_and_the_blocker(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())
        update_node(tmp_path, "s", "N1", resolution="in_progress")

        text = render_board_summary(load_board(tmp_path, "s"))

        assert "board task-" in text
        assert "resolution: in_progress=1 open=2" in text
        assert "in progress (1):" in text
        # N1 in progress ⇒ N2 waits on it and N3 waits on N2: both blocked.
        assert "blocked (2):" in text
        assert "blocked_by: N1" in text

    def test_summary_derives_readiness_like_the_board(self, tmp_path):
        """The render's frontier must match next_ready_nodes, not a stale field."""
        create_board(tmp_path, "s", "obj", _nodes())
        update_node(tmp_path, "s", "N1", resolution="resolved")

        board = load_board(tmp_path, "s")
        text = render_board_summary(board)
        ready_ids = [n["node_id"] for n in next_ready_nodes(tmp_path, "s")["ready"]]

        assert ready_ids == ["N2"]
        assert "ready frontier (1):" in text
        assert text.index("N2") < text.index("resolved (1):")


class TestReportStatusIsAdvisory:
    """A child's own label is recorded, never a state transition.

    AgentOS: "A subagent execution may be reported while its item remains open
    because the report is incomplete, contradicted, or awaiting verification"
    (§3.3.2). Resolving is a separate, explicit act.
    """

    def test_success_label_does_not_advance_resolution(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())
        update_node(tmp_path, "s", "N1", resolution="in_progress")

        result = attach_report(tmp_path, "s", "N1", {"status": "success"})

        assert result["node"]["report_status"] == "success"
        assert result["node"]["resolution"] == "in_progress"

    def test_partial_label_is_recorded_as_partial(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())

        result = attach_report(tmp_path, "s", "N1", {"status": "partial"})

        assert result["node"]["report_status"] == "partial"
        assert result["node"]["resolution"] == "open"

    def test_unrecognised_label_is_kept_not_rejected(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())

        result = attach_report(tmp_path, "s", "N1", {"status": "Mostly Fine"})

        assert result["node"]["report_status"] == "mostly fine"

    def test_missing_label_is_unknown(self, tmp_path):
        create_board(tmp_path, "s", "obj", _nodes())

        result = attach_report(tmp_path, "s", "N1", {"claims": []})

        assert result["node"]["report_status"] == "unknown"


class TestConcurrentWrites:
    def test_parallel_updates_all_land(self, tmp_path):
        """The runtime runs a turn's independent tool calls on worker threads.
        Before the board lock, load -> mutate -> save silently dropped updates
        (measured: 6 concurrent updates left 2 applied and zero errors)."""
        create_board(
            tmp_path, "s", "obj", [{"node_id": f"N{i}", "goal": "g"} for i in range(1, 7)]
        )

        def worker(index):
            update_node(tmp_path, "s", f"N{index}", resolution="in_progress")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 7)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        board = load_board(tmp_path, "s")
        started = [
            n["node_id"] for n in board["nodes"] if n["resolution"] == "in_progress"
        ]
        assert started == [f"N{i}" for i in range(1, 7)]


class TestVerificationGate:
    """`resolved` asserts "returned AND sufficiently checked". With
    longtask.require_verification_before_unlock on, that claim needs an accepted
    verdict; with the gate off (the default) it is the agent's call alone."""

    def _set_gate(self, monkeypatch, enabled: bool):
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: (
                {"longtask": {"require_verification_before_unlock": True}}
                if enabled
                else {"longtask": {}}
            ),
        )

    def test_off_by_default(self, tmp_path, monkeypatch):
        self._set_gate(monkeypatch, enabled=False)
        create_board(tmp_path, "s", "obj", _nodes())

        assert (
            update_node(tmp_path, "s", "N1", resolution="resolved")["node"]["resolution"]
            == "resolved"
        )

    def test_blocks_resolved_without_a_verdict(self, tmp_path, monkeypatch):
        self._set_gate(monkeypatch, enabled=True)
        create_board(tmp_path, "s", "obj", _nodes())

        with pytest.raises(LongtaskBoardError, match="verification"):
            update_node(tmp_path, "s", "N1", resolution="resolved")

    def test_blocks_resolved_on_a_non_accepted_verdict(self, tmp_path, monkeypatch):
        self._set_gate(monkeypatch, enabled=True)
        create_board(tmp_path, "s", "obj", _nodes())
        update_node(tmp_path, "s", "N1", verification={"verdict": "needs_followup"})

        with pytest.raises(LongtaskBoardError, match="needs_followup"):
            update_node(tmp_path, "s", "N1", resolution="resolved")

    def test_attach_never_resolves_even_with_the_gate_on(self, tmp_path, monkeypatch):
        self._set_gate(monkeypatch, enabled=True)
        create_board(tmp_path, "s", "obj", _nodes())

        result = attach_report(tmp_path, "s", "N1", {"status": "success"})

        assert result["node"]["execution"] == "reported"
        assert result["node"]["resolution"] == "open"

    def test_accepted_verdict_then_resolved_unlocks_dependents(self, tmp_path, monkeypatch):
        self._set_gate(monkeypatch, enabled=True)
        create_board(tmp_path, "s", "obj", _nodes())
        update_node(tmp_path, "s", "N1", verification={"verdict": "accepted"})

        assert (
            update_node(tmp_path, "s", "N1", resolution="resolved")["node"]["resolution"]
            == "resolved"
        )
        assert [n["node_id"] for n in next_ready_nodes(tmp_path, "s")["ready"]] == ["N2"]
