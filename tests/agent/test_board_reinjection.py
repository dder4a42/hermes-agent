"""Behaviour contracts for event-driven long-horizon board re-injection (P4).

The board must stay visible to the agent WITHOUT a timer and WITHOUT touching the
prompt cache, and a run must not silently wrap up while the board still has
unresolved items. These tests drive the real decision functions and the real
carrier (the newest tool result's body) against a real on-disk board — no
source-text assertions, no change-detector snapshots.
"""

import logging
from types import SimpleNamespace

import pytest

from agent import longtask_reinjection as reinj
from agent.longtask_board import add_nodes, create_board, load_board, update_node

SESSION = "s-reinject"


def _nodes():
    return [
        {"node_id": "N1", "goal": "Research constraints"},
        {"node_id": "N2", "goal": "Implement feature", "dependencies": ["N1"]},
    ]


def _batch(tool_name, result='{"status": "ok"}'):
    """One assistant tool-call plus its tool result — the shape the loop posts."""
    return [
        {"role": "user", "content": "keep going"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": result},
    ]


def _same_role_adjacent(messages):
    roles = [m.get("role") for m in messages if isinstance(m, dict)]
    return any(a == b for a, b in zip(roles, roles[1:]))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # The board resolver probes TERMINAL_CWD first (agent/longtask_tool.py), so
    # pinning it keeps the agent, the tools, and the compression engine on the
    # same board root.
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    return tmp_path


@pytest.fixture
def board_root(workspace):
    return workspace / ".hermes" / "tasks"


@pytest.fixture
def agent(monkeypatch):
    cfg = {}
    monkeypatch.setattr(reinj, "load_longtask_config", lambda: dict(cfg))
    return SimpleNamespace(session_id=SESSION, _config=cfg)


def _seed_injection(agent, board_root, tool_name="longtask_read"):
    """Run one real injection so the agent state is primed; return the tail text."""
    messages = _batch(tool_name)
    assert reinj.maybe_reinject_board(agent, messages) is True
    return messages[-1]["content"]


# ---------------------------------------------------------------------------
# (a) unchanged board ⇒ byte-identical tool result
# ---------------------------------------------------------------------------


def test_unchanged_board_leaves_the_tool_result_byte_identical(agent, board_root):
    create_board(board_root, SESSION, "obj", _nodes())

    tail_after_first = _seed_injection(agent, board_root)
    assert "[long-horizon board] snapshot #1" in tail_after_first

    # Nothing about the board changed, so the next tool result must be produced
    # byte-for-byte identically — no second render, no whitespace churn.
    messages = _batch("longtask_read")
    assert reinj.maybe_reinject_board(agent, messages) is False
    assert messages[-1]["content"] == '{"status": "ok"}'


def test_noop_injection_emits_no_log_record(agent, board_root, caplog):
    create_board(board_root, SESSION, "obj", _nodes())
    _seed_injection(agent, board_root)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="agent.longtask_reinjection"):
        assert reinj.maybe_reinject_board(agent, _batch("longtask_read")) is False
    # An operator must be able to tell an injection from a no-op.
    assert caplog.records == []


def test_injection_logs_reason_and_sequence(agent, board_root, caplog):
    create_board(board_root, SESSION, "obj", _nodes())

    with caplog.at_level(logging.INFO, logger="agent.longtask_reinjection"):
        assert reinj.maybe_reinject_board(agent, _batch("longtask_read")) is True

    assert "reason=material_change" in caplog.text
    assert "seq=1" in caplog.text


# ---------------------------------------------------------------------------
# (b) material change ⇒ exactly ONE injection, no user-role message
# ---------------------------------------------------------------------------


def test_material_change_injects_exactly_once_into_next_tool_result(agent, board_root):
    create_board(board_root, SESSION, "obj", _nodes())
    _seed_injection(agent, board_root)
    assert agent._board_reinjection_state.last_digest == reinj.snapshot_digest(
        load_board(board_root, SESSION)
    )

    # A node resolves (and its dependent unlocks) — a material change.
    update_node(board_root, SESSION, "N1", resolution="resolved")

    messages = _batch("longtask_update_node")
    users_before = [m for m in messages if m.get("role") == "user"]
    count_before = len(messages)

    assert reinj.maybe_reinject_board(agent, messages) is True

    tail = messages[-1]["content"]
    assert tail.count("[long-horizon board] snapshot #") == 1
    assert "snapshot #2" in tail
    assert "[end board snapshot #2]" in tail

    # Carrier-only: no synthetic user message, no new message rows.
    assert [m for m in messages if m.get("role") == "user"] == users_before
    assert len(messages) == count_before

    # And the event does not re-fire once the render is current.
    nxt = _batch("longtask_update_node")
    assert reinj.maybe_reinject_board(agent, nxt) is False
    assert nxt[-1]["content"] == '{"status": "ok"}'


def test_verdict_landing_injects_the_new_verdict(agent, board_root):
    """The other event P4 names: a verdict lands (verify_node is board contact)."""
    create_board(board_root, SESSION, "obj", _nodes())
    _seed_injection(agent, board_root)

    update_node(
        board_root,
        SESSION,
        "N1",
        verification={"verdict": "accepted", "reason": "checked"},
    )

    messages = _batch("longtask_verify_node")
    assert reinj.maybe_reinject_board(agent, messages) is True
    assert "accepted" in messages[-1]["content"]


def test_dependent_unlock_is_visible_on_the_ready_frontier(agent, board_root):
    create_board(board_root, SESSION, "obj", _nodes())
    _seed_injection(agent, board_root)

    # N2 is blocked until N1 resolves; resolving N1 unlocks it.
    update_node(board_root, SESSION, "N1", resolution="resolved")
    messages = _batch("longtask_update_node")
    assert reinj.maybe_reinject_board(agent, messages) is True

    tail = messages[-1]["content"]
    assert "ready frontier" in tail
    assert "N2" in tail


def test_snapshot_reads_as_a_snapshot_not_state(agent, board_root):
    create_board(board_root, SESSION, "obj", _nodes())
    tail = _seed_injection(agent, board_root)

    assert "snapshot #1" in tail
    assert "source of truth" in tail
    assert "N1" in tail and "N2" in tail


def test_verdict_landing_changes_the_digest(board_root):
    """A verdict is a material event even before the item is marked resolved."""
    create_board(board_root, SESSION, "obj", _nodes())
    before = reinj.snapshot_digest(load_board(board_root, SESSION))

    update_node(
        board_root,
        SESSION,
        "N1",
        verification={"verdict": "accepted", "reason": "checked"},
    )

    board = load_board(board_root, SESSION)
    assert reinj.snapshot_digest(board) != before
    # ...and the model can actually SEE what changed, not just a different hash.
    assert "accepted" in reinj.snapshot_body(board)


def test_write_time_noise_is_not_a_material_change(board_root):
    """`updated_at` alone must not read as a change, or every save re-injects."""
    board = create_board(board_root, SESSION, "obj", _nodes())
    digest = reinj.snapshot_digest(board)

    board["updated_at"] = "2099-01-01T00:00:00Z"
    assert reinj.snapshot_digest(board) == digest


# ---------------------------------------------------------------------------
# (c) idle fallback fires after K board-less turns, and NOT before
# ---------------------------------------------------------------------------


def test_idle_fallback_fires_after_k_turns_without_board_contact(agent, board_root):
    agent._config.update({"board_reinject_idle_turns": 3})
    create_board(board_root, SESSION, "obj", _nodes())
    _seed_injection(agent, board_root)

    # The board changes WITHOUT the model touching it (a fan-in / host write).
    add_nodes(board_root, SESSION, [{"node_id": "N3", "goal": "Follow-up"}])
    state = agent._board_reinjection_state

    for turn in (1, 2):
        messages = _batch("terminal")  # a board-less tool batch
        assert reinj.maybe_reinject_board(agent, messages) is False
        assert "snapshot #" not in messages[-1]["content"]
        assert state.turns_since_contact == turn

    messages = _batch("terminal")  # turn K == 3 -> the safety net fires
    assert reinj.maybe_reinject_board(agent, messages) is True
    assert "[long-horizon board] snapshot #2 (idle reminder)" in messages[-1]["content"]
    # Emitting counts as fresh board contact: the net restarts its countdown.
    assert state.turns_since_contact == 0


def test_idle_fallback_is_suppressed_when_nothing_changed(agent, board_root):
    agent._config.update({"board_reinject_idle_turns": 2})
    create_board(board_root, SESSION, "obj", _nodes())
    _seed_injection(agent, board_root)

    # The board did not change, so even well past K the hash dedup wins — an
    # unchanged board must never grow a tool result.
    state = agent._board_reinjection_state
    state.turns_since_contact = 50
    messages = _batch("terminal")
    assert reinj.maybe_reinject_board(agent, messages) is False
    assert messages[-1]["content"] == '{"status": "ok"}'


def test_board_contact_resets_the_idle_counter(agent, board_root):
    create_board(board_root, SESSION, "obj", _nodes())
    _seed_injection(agent, board_root)
    state = agent._board_reinjection_state

    reinj.maybe_reinject_board(agent, _batch("terminal"))
    reinj.maybe_reinject_board(agent, _batch("terminal"))
    assert state.turns_since_contact == 2

    reinj.maybe_reinject_board(agent, _batch("longtask_next"))
    assert state.turns_since_contact == 0


# ---------------------------------------------------------------------------
# (d) finalization gate: soft warns, hard refuses at most once
# ---------------------------------------------------------------------------


def test_soft_gate_surfaces_unresolved_items_without_changing_the_work(agent, board_root):
    agent._config.update({"enforce_finalization_gate": "warn"})
    create_board(board_root, SESSION, "obj", _nodes())

    messages = _batch("longtask_next")
    count_before = len(messages)
    response = "All done!"

    final, continue_turn = reinj.apply_final_gate(agent, messages, response)

    assert continue_turn is False
    assert final == response  # the delivered work is untouched
    assert len(messages) == count_before

    tail = messages[-1]["content"]
    assert "N1" in tail and "N2" in tail
    assert "unresolved items" in tail


def test_hard_gate_takes_at_most_one_extra_bounded_turn(agent, board_root):
    agent._config.update({"enforce_finalization_gate": "hard"})
    create_board(board_root, SESSION, "obj", _nodes())

    decisions = []
    for _ in range(5):
        messages = _batch("longtask_next")
        response = "Done."
        final, continue_turn = reinj.apply_final_gate(agent, messages, response)
        decisions.append(continue_turn)
        if continue_turn:
            assert "wrap-up refused" in messages[-1]["content"]
            assert final == response
        else:
            # Honoured: the model's answer is what ships.
            assert final == response

    assert decisions[0] is True
    assert decisions.count(True) == 1  # never a loop
    assert not any(decisions[1:])


def test_gate_is_silent_when_the_board_is_clean(agent, board_root):
    agent._config.update({"enforce_finalization_gate": "hard"})
    create_board(board_root, SESSION, "obj", _nodes())
    update_node(board_root, SESSION, "N1", resolution="resolved")
    update_node(board_root, SESSION, "N2", resolution="resolved")

    messages = _batch("longtask_next")
    final, continue_turn = reinj.apply_final_gate(agent, messages, "Done.")

    assert continue_turn is False
    assert final == "Done."
    assert messages[-1]["content"] == '{"status": "ok"}'


def test_gate_mode_off_never_touches_anything(agent, board_root):
    agent._config.update({"enforce_finalization_gate": "off"})
    create_board(board_root, SESSION, "obj", _nodes())

    messages = _batch("longtask_next")
    final, continue_turn = reinj.apply_final_gate(agent, messages, "Done.")

    assert continue_turn is False
    assert final == "Done."
    assert messages[-1]["content"] == '{"status": "ok"}'


def test_gate_ignores_a_dropped_tool_call_response(agent, board_root):
    """finish_reason=tool_calls with no calls is not a genuine wrap-up."""
    create_board(board_root, SESSION, "obj", _nodes())

    messages = _batch("longtask_next")
    final, continue_turn = reinj.apply_final_gate(
        agent, messages, "Narrating the plan", finish_reason="tool_calls"
    )

    assert continue_turn is False
    assert final == "Narrating the plan"
    assert messages[-1]["content"] == '{"status": "ok"}'


# ---------------------------------------------------------------------------
# (e) role alternation + append-only invariants
# ---------------------------------------------------------------------------


def test_injection_preserves_role_alternation_and_message_count(agent, board_root):
    create_board(board_root, SESSION, "obj", _nodes())

    messages = _batch("longtask_update_node")
    roles_before = [m["role"] for m in messages]
    count_before = len(messages)
    prefix_before = [m.get("content") for m in messages[:-1]]

    assert reinj.maybe_reinject_board(agent, messages) is True

    assert len(messages) == count_before  # the carrier grew in place
    assert [m["role"] for m in messages] == roles_before
    assert not _same_role_adjacent(messages)
    # Nothing before the newest tool result was touched (append-only).
    assert [m.get("content") for m in messages[:-1]] == prefix_before
    assert "snapshot #" in messages[-1]["content"]


def test_final_gate_preserves_role_alternation_and_message_count(agent, board_root):
    agent._config.update({"enforce_finalization_gate": "hard"})
    create_board(board_root, SESSION, "obj", _nodes())

    messages = _batch("longtask_next")
    roles_before = [m["role"] for m in messages]

    _, continue_turn = reinj.apply_final_gate(agent, messages, "Done.")

    assert continue_turn is True
    assert [m["role"] for m in messages] == roles_before
    assert not _same_role_adjacent(messages)
    assert len(messages) == 3


def test_carrier_refuses_to_reach_back_into_past_context():
    """An injection must never mutate a row the model already answered from."""
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "old result"},
        {"role": "user", "content": "another"},
    ]
    before = [m.get("content") for m in messages]

    assert reinj.inject_into_newest_tool_result(messages, "SNAPSHOT") is False
    assert [m.get("content") for m in messages] == before


# ---------------------------------------------------------------------------
# Knobs + carrier details
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Carrier details
# ---------------------------------------------------------------------------


def test_conversation_loop_wiring_reinjects_and_gates(agent, board_root):
    """The loop's thin wrappers reach the module — the wiring, not just the logic."""
    from agent import conversation_loop

    agent._config.update({"enforce_finalization_gate": "hard"})
    create_board(board_root, SESSION, "obj", _nodes())

    messages = _batch("longtask_read")
    assert conversation_loop._maybe_reinject_board(agent, messages) is True
    assert "[long-horizon board] snapshot #1" in messages[-1]["content"]

    messages2 = _batch("longtask_next")
    final, continue_turn = conversation_loop._apply_board_final_gate(
        agent, messages2, "Done.", "stop"
    )
    assert continue_turn is True
    assert "wrap-up refused" in messages2[-1]["content"]


def test_clamp_idle_turns():
    assert reinj.clamp_idle_turns(0) == reinj.IDLE_TURNS_FLOOR
    assert reinj.clamp_idle_turns(-7) == reinj.IDLE_TURNS_FLOOR
    assert reinj.clamp_idle_turns(10_000) == reinj.IDLE_TURNS_CEILING
    assert reinj.clamp_idle_turns(4) == 4
    assert reinj.clamp_idle_turns("6") == 6
    assert reinj.clamp_idle_turns(None) == reinj.DEFAULT_IDLE_TURNS
    assert reinj.clamp_idle_turns(True) == reinj.DEFAULT_IDLE_TURNS


def test_resolve_final_gate_mode():
    assert reinj.resolve_final_gate_mode(None) == "warn"
    assert reinj.resolve_final_gate_mode("warn") == "warn"
    assert reinj.resolve_final_gate_mode("HARD") == "hard"
    assert reinj.resolve_final_gate_mode("off") == "off"
    assert reinj.resolve_final_gate_mode(True) == "hard"
    assert reinj.resolve_final_gate_mode(False) == "off"
    assert reinj.resolve_final_gate_mode("nonsense") == "warn"


def test_latest_batch_tool_names_reads_the_newest_batch():
    messages = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "terminal"}}]},
        {"role": "tool", "content": "out"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "longtask_next"}}],
        },
        {"role": "tool", "content": "board"},
    ]
    assert reinj.latest_batch_tool_names(messages) == {"longtask_next"}

    final = messages + [{"role": "assistant", "content": "done"}]
    assert reinj.latest_batch_tool_names(final) == set()


def test_carrier_handles_multimodal_content_blocks():
    messages = [
        {"role": "user", "content": "x"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "longtask_read"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "hi"}]},
    ]

    assert reinj.inject_into_newest_tool_result(messages, "SNAPSHOT") is True
    assert messages[-1]["content"][-1] == {"type": "text", "text": "SNAPSHOT"}
    assert messages[-1]["content"][0] == {"type": "text", "text": "hi"}
