#!/usr/bin/env python3
"""Host-mediated backfill of delegated reports onto the long-horizon board.

A batch item may declare ``node_id``: the HOST then attaches that child's
schema-valid structured report to the board item itself when the child
finishes, with provenance. Before this, a report reached the board only when
the PARENT MODEL noticed the child's result and called
``longtask_attach_report`` itself — the per-child bookkeeping that a long run
skips, leaving dispatched items with no report at all (AgentOS §3.2/3.3).

Contracts pinned here:
  * attach records the report (``execution=reported``) and NEVER advances
    ``resolution`` — that judgement stays the coordinator's, with its own
    evidence;
  * provenance (child_session_id / task_index / delegation_id / report_id) is
    recorded on the item AND on the result entry the parent model reads;
  * no board node declared => the board is untouched;
  * no schema-valid report => no attach, and the reason is on the entry;
  * a missing/unusable board never breaks a delegation.

Board root/session are resolved through the SAME helpers the ``longtask_*``
tools use (``tools.longtask_tool._root`` / ``_session_id``), so the tests drive
the real resolution against a temp workspace instead of patching paths.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.longtask_board import create_board, load_board
from tools.delegate_tool import _finalize_child_results, _run_single_child

NODE_ID = "N1"
VALID_REPORT_TEXT = '{"city": "Berlin", "zip": "10115"}'
# Not JSON at all: schema_valid goes False in every environment (with or
# without the optional jsonschema dependency), which is the contract under test.
INVALID_REPORT_TEXT = "I could not produce the structured report."

REPORT_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "zip": {"type": "string"}},
    "required": ["city"],
}


class _StubChild:
    """Minimal child agent double (mirrors test_delegate_output_schema)."""

    tool_progress_callback = None
    _delegate_saved_tool_names: list = []
    _credential_pool = None
    _subagent_id = None  # skip the live registry
    _delegate_depth = 1
    _parent_subagent_id = None
    _delegate_output_schema = None
    _delegate_role = "leaf"
    _delegation_id = "deleg_batch1"
    model = "test-model"
    session_id = "child-session-1"
    session_prompt_tokens = 0
    session_completion_tokens = 0
    session_estimated_cost_usd = 0.0
    session_reasoning_tokens = 0

    def __init__(self, response, **result_overrides):
        self.response = response
        self.result_overrides = result_overrides
        self.calls = []

    def get_activity_summary(self):
        return {"api_call_count": 1, "max_iterations": 5, "current_tool": None}

    def run_conversation(self, user_message, task_id=None, **_kwargs):
        self.calls.append(user_message)
        return {
            "final_response": self.response,
            "completed": True,
            "api_calls": 1,
            "messages": [],
            **self.result_overrides,
        }

    def close(self):
        return None


class _StubParent:
    _current_task_id = None
    _delegate_depth = 0
    session_id = "parent-session-1"

    def _touch_activity(self, _desc):
        return None


@pytest.fixture
def parent(tmp_path, monkeypatch):
    """A parent whose workspace (and thus board root) is a temp dir."""
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    # Real plugin hooks have no business firing in a unit test.
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **k: None)
    agent = _StubParent()
    agent.terminal_cwd = str(tmp_path)
    agent._memory_manager = Mock()
    return agent


@pytest.fixture
def board_root(tmp_path):
    return tmp_path / ".hermes" / "tasks"


def _board(board_root, parent):
    return create_board(
        board_root,
        parent.session_id,
        "Ship the host-mediated backfill",
        [{"node_id": NODE_ID, "goal": "Produce the structured report"}],
    )


def _run(parent, text, task):
    """Run the real child-result path, then the real host finalization."""
    child = _StubChild(text)
    child._delegate_output_schema = REPORT_SCHEMA
    entry = _run_single_child(0, task["goal"], child, parent)
    _finalize_child_results([entry], [task], [(0, task, child)], parent)
    return entry, child


def _node(board_root, parent):
    return load_board(board_root, parent.session_id)["nodes"][0]


def test_declared_node_receives_report_with_provenance(board_root, parent, monkeypatch):
    """E2E: batch item declares node_id => the HOST attaches the report.

    No model tool call is involved: the tool dispatcher itself is booby trapped,
    so the node can only be populated by host finalization.
    """

    def _forbidden_tool_call(*_args, **_kwargs):
        raise AssertionError("a model tool call ran — the host must attach directly")

    monkeypatch.setattr("tools.registry.registry.dispatch", _forbidden_tool_call)
    _board(board_root, parent)
    task = {"goal": "Produce the structured report", "node_id": NODE_ID}

    entry, child = _run(parent, VALID_REPORT_TEXT, task)

    node = _node(board_root, parent)
    assert node["execution"] == "reported"
    assert node["resolution"] == "open"  # attaching never resolves the item
    # Provenance: which child, which task slot, which delegation run, which
    # persisted report — the ids the parent can cite later.
    assert node["provenance"]["child_session_id"] == "child-session-1"
    assert node["provenance"]["task_index"] == 0
    assert node["provenance"]["delegation_id"] == "deleg_batch1"
    assert node["provenance"]["report_id"] == entry["report_id"]
    assert entry["report_id"]
    assert entry["board_node_id"] == NODE_ID
    assert entry["delegation_id"] == "deleg_batch1"
    assert entry["child_session_id"] == "child-session-1"
    assert "_structured_report" not in entry  # internal staging, never echoed
    # The child answered exactly once and ran no tool: the report came from its
    # own final message, not from a tool call by anyone.
    assert len(child.calls) == 1
    # The persisted report is the parsed child object.
    report = json.loads(
        (board_root / parent.session_id / node["report_path"]).read_text(encoding="utf-8")
    )
    assert report["city"] == "Berlin"


def test_provenance_ids_reach_the_memory_provider(board_root, parent):
    """The same ids ride the on_delegation call, as kwargs."""
    _board(board_root, parent)
    task = {"goal": "Produce the structured report", "node_id": NODE_ID}

    entry, _child = _run(parent, VALID_REPORT_TEXT, task)

    kwargs = parent._memory_manager.on_delegation.call_args.kwargs
    assert kwargs["child_session_id"] == "child-session-1"
    assert kwargs["task_index"] == 0
    assert kwargs["delegation_id"] == entry["delegation_id"]
    assert kwargs["report_id"] == entry["report_id"]
    assert kwargs["board_node_id"] == NODE_ID


def test_schema_invalid_child_is_not_attached_and_says_why(board_root, parent):
    _board(board_root, parent)
    task = {"goal": "Produce the structured report", "node_id": NODE_ID}

    entry, _child = _run(parent, INVALID_REPORT_TEXT, task)

    node = _node(board_root, parent)
    assert node["execution"] == "none"
    assert node.get("report_path") is None
    assert node["resolution"] == "open"
    # The skip is observable on the entry rather than silent — a silent skip is
    # the failure mode this feature exists to remove.
    assert "board_attach_skipped" in entry
    assert NODE_ID in entry["board_attach_skipped"]
    assert "report_id" not in entry
    assert "_structured_report" not in entry


def test_failed_child_is_not_attached_and_says_why(board_root, parent):
    """A child that produced nothing has no report to record."""
    _board(board_root, parent)
    task = {"goal": "Produce the structured report", "node_id": NODE_ID}
    child = _StubChild("")  # no final answer => status "failed"
    child._delegate_output_schema = REPORT_SCHEMA
    entry = _run_single_child(0, task["goal"], child, parent)
    assert entry["status"] == "failed"

    _finalize_child_results([entry], [task], [(0, task, child)], parent)

    assert _node(board_root, parent)["execution"] == "none"
    assert "board_attach_skipped" in entry
    assert "failed" in entry["board_attach_skipped"]


def test_interrupted_child_with_valid_report_is_not_attached(board_root, parent):
    """An interrupted run's text is an artifact, not a delivered report.

    The item stays untouched so the coordinator re-dispatches it rather than
    reading a mid-interrupt answer as the item's result.
    """
    _board(board_root, parent)
    task = {"goal": "Produce the structured report", "node_id": NODE_ID}
    child = _StubChild(VALID_REPORT_TEXT, interrupted=True, completed=False)
    child._delegate_output_schema = REPORT_SCHEMA
    entry = _run_single_child(0, task["goal"], child, parent)
    assert entry["status"] == "interrupted"

    _finalize_child_results([entry], [task], [(0, task, child)], parent)

    assert _node(board_root, parent)["execution"] == "none"
    assert "interrupted" in entry["board_attach_skipped"]


def test_task_without_node_id_leaves_the_board_untouched(board_root, parent):
    _board(board_root, parent)
    board_path = board_root / parent.session_id / "board.json"
    before = board_path.read_bytes()
    task = {"goal": "Produce the structured report"}  # no node binding

    entry, _child = _run(parent, VALID_REPORT_TEXT, task)

    assert board_path.read_bytes() == before
    assert _node(board_root, parent)["execution"] == "none"
    # Nothing board-shaped appears on the result the model reads.
    assert "board_node_id" not in entry
    assert "board_attach_skipped" not in entry
    assert "report_id" not in entry
    assert "delegation_id" not in entry
    assert "_structured_report" not in entry


def test_unknown_node_is_reported_not_raised(board_root, parent):
    """node_id shape is validated in the batch gate; EXISTENCE is attach-time.

    A stale/typo'd node must not fail a delegation whose work still succeeded.
    """
    _board(board_root, parent)
    task = {"goal": "Produce the structured report", "node_id": "N-MISSING"}

    entry, _child = _run(parent, VALID_REPORT_TEXT, task)

    assert entry["status"] == "completed"
    assert entry["summary"] == VALID_REPORT_TEXT
    assert "N-MISSING" in entry["board_attach_skipped"]
    # The declared node was left alone; no stray node was created either.
    assert [n["node_id"] for n in load_board(board_root, parent.session_id)["nodes"]] == [
        NODE_ID
    ]


def test_missing_board_does_not_change_the_delegation_result(tmp_path, monkeypatch):
    """Failure isolation: no board for this session => result unchanged."""
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **k: None)
    parent = _StubParent()
    parent.terminal_cwd = str(tmp_path)  # .hermes/tasks/<sid> never created
    task = {"goal": "Produce the structured report", "node_id": NODE_ID}

    child = _StubChild(VALID_REPORT_TEXT)
    child._delegate_output_schema = REPORT_SCHEMA
    entry = _run_single_child(0, task["goal"], child, parent)
    before = {k: v for k, v in entry.items() if not k.startswith("_")}

    _finalize_child_results([entry], [task], [(0, task, child)], parent)

    after = {
        k: v
        for k, v in entry.items()
        if not k.startswith("_") and k != "board_attach_skipped"
    }
    assert after == before
    assert entry["status"] == "completed"
    assert "board_attach_skipped" in entry


def test_unusable_board_root_does_not_change_the_delegation_result(tmp_path, monkeypatch):
    """Failure isolation: board root cannot even be created => result unchanged."""
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *a, **k: None)
    # A FILE where the workspace should be: every board path below it fails.
    workspace_file = tmp_path / "not-a-directory"
    workspace_file.write_text("x", encoding="utf-8")
    parent = _StubParent()
    parent.terminal_cwd = str(workspace_file)
    task = {"goal": "Produce the structured report", "node_id": NODE_ID}

    child = _StubChild(VALID_REPORT_TEXT)
    child._delegate_output_schema = REPORT_SCHEMA
    entry = _run_single_child(0, task["goal"], child, parent)
    before = {k: v for k, v in entry.items() if not k.startswith("_")}

    _finalize_child_results([entry], [task], [(0, task, child)], parent)

    after = {
        k: v
        for k, v in entry.items()
        if not k.startswith("_") and k != "board_attach_skipped"
    }
    assert after == before
    assert "board_attach_skipped" in entry


def test_fanout_shares_one_delegation_id(board_root, parent):
    """One delegation run, N children: every attached item names the same run."""
    create_board(
        board_root,
        parent.session_id,
        "Ship the host-mediated backfill",
        [
            {"node_id": "N1", "goal": "Produce the first report"},
            {"node_id": "N2", "goal": "Produce the second report"},
        ],
    )
    task_a = {"goal": "Produce the first report", "node_id": "N1"}
    task_b = {"goal": "Produce the second report", "node_id": "N2"}
    child_a, child_b = _StubChild(VALID_REPORT_TEXT), _StubChild(VALID_REPORT_TEXT)
    child_a._delegate_output_schema = REPORT_SCHEMA
    child_b._delegate_output_schema = REPORT_SCHEMA
    child_a._delegation_id = ""  # neither child carries a run id of its own
    child_b._delegation_id = ""
    entry_a = _run_single_child(0, task_a["goal"], child_a, parent)
    entry_b = _run_single_child(1, task_b["goal"], child_b, parent)

    _finalize_child_results(
        [entry_a, entry_b],
        [task_a, task_b],
        [(0, task_a, child_a), (1, task_b, child_b)],
        parent,
    )

    nodes = {n["node_id"]: n for n in load_board(board_root, parent.session_id)["nodes"]}
    assert nodes["N1"]["execution"] == "reported"
    assert nodes["N2"]["execution"] == "reported"
    assert nodes["N1"]["provenance"]["delegation_id"]
    assert (
        nodes["N1"]["provenance"]["delegation_id"]
        == nodes["N2"]["provenance"]["delegation_id"]
        == entry_a["delegation_id"]
        == entry_b["delegation_id"]
    )
    assert nodes["N1"]["provenance"]["task_index"] == 0
    assert nodes["N2"]["provenance"]["task_index"] == 1


def test_explicit_batch_delegation_id_wins(board_root, parent):
    """A run that already carries an id (live/async registry) reuses it."""
    _board(board_root, parent)
    task = {"goal": "Produce the structured report", "node_id": NODE_ID}
    child = _StubChild(VALID_REPORT_TEXT)
    child._delegate_output_schema = REPORT_SCHEMA
    child._delegation_id = ""
    entry = _run_single_child(0, task["goal"], child, parent)

    _finalize_child_results(
        [entry], [task], [(0, task, child)], parent, delegation_id="deleg_run42"
    )

    assert entry["delegation_id"] == "deleg_run42"
    assert _node(board_root, parent)["provenance"]["delegation_id"] == "deleg_run42"


def test_provenance_survives_a_board_reload(board_root, parent):
    """The board is load -> normalize -> save: provenance must not drop."""
    _board(board_root, parent)
    task = {"goal": "Produce the structured report", "node_id": NODE_ID}
    entry, _child = _run(parent, VALID_REPORT_TEXT, task)

    reloaded = _node(board_root, parent)

    assert reloaded["provenance"]["report_id"] == entry["report_id"]


def test_reported_entry_without_schema_is_not_attached(board_root, parent):
    """A node binding without output_schema has no structured report to attach."""
    _board(board_root, parent)
    task = {"goal": "Produce the structured report", "node_id": NODE_ID}
    child = _StubChild(VALID_REPORT_TEXT)  # no _delegate_output_schema
    entry = _run_single_child(0, task["goal"], child, parent)

    _finalize_child_results([entry], [task], [(0, task, child)], parent)

    assert _node(board_root, parent)["execution"] == "none"
    assert "board_attach_skipped" in entry
    assert "schema_valid" not in entry
