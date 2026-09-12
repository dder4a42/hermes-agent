"""Event-driven long-horizon board re-injection and the finalization gate (P4).

Apodex §3.3 wants the task board (``K_t``) visible to the coordinator while a run
proceeds (docs/plans/agentos-collaboration.md, "P4"). Two invariants rule out the
obvious implementations:

* **No timer.** A render appended on a clock lives in the transcript and is
  re-sent on every later API call until compression, so its cost grows
  quadratically over a long run.
* **No cache break.** Rewriting an older render to keep "only the latest board"
  mutates past context and invalidates the per-conversation prompt-cache prefix —
  compression is the one sanctioned exception (AGENTS.md).

So re-injection is **event-driven** and rides a tool result the model already
receives (the same steering channel ``apply_pending_steer_to_tool_results`` uses
for ``/steer`` and ``_maybe_inject_run_budget_wrapup`` uses for a run budget),
and it is suppressed wholesale when the rendered board is unchanged since the
last injection. A run-scoped "the model has stopped looking at the board"
fallback covers the case where the board changed without the model touching it.
A finalization gate surfaces still-unresolved items at wrap-up instead of letting
a run end silently.

This module holds the decision logic as pure functions so the behaviour can be
tested honestly — the conversation loop only wires them to the newest tool
result. Everything here is append-only: no message is inserted, no synthetic
user message is created, and nothing before the newest tool result is touched.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# A tool batch that touched the board (or produced the fan-in completion of a
# delegation) already puts a fresh result in front of the model, so appending a
# snapshot to it costs essentially nothing. `delegate_task` is included because
# its fan-in result is the point where a host-attached report (P2) can change the
# board without any longtask_* call.
BOARD_CONTACT_TOOL_NAMES = frozenset(
    {
        "longtask_create",
        "longtask_add_node",
        "longtask_cancel_node",
        "longtask_read",
        "longtask_next",
        "longtask_update_node",
        "longtask_attach_report",
        "longtask_verify_node",
        "delegate_task",
    }
)

REASON_MATERIAL = "material_change"
REASON_IDLE = "idle_fallback"
REASON_FINAL_GATE = "final_gate"

# `longtask.board_reinject_idle_turns` — how many consecutive board-less turns
# may pass, while items remain unresolved, before one safety-net render is
# emitted. Clamped so a hand-edited config can neither disable the net (0) nor
# make it a clock in disguise.
DEFAULT_IDLE_TURNS = 5
IDLE_TURNS_FLOOR = 1
IDLE_TURNS_CEILING = 1000

# `longtask.enforce_finalization_gate` — "warn" (default, surface and finish),
# "hard" (refuse the wrap-up once, then honour the model), or "off".
FINAL_GATE_MODES = ("off", "warn", "hard")
DEFAULT_FINAL_GATE_MODE = "warn"

UNRESOLVED_RESOLUTIONS = frozenset({"open", "in_progress"})

_TRIGGER_LABELS = {
    REASON_MATERIAL: "material change",
    REASON_IDLE: "idle reminder",
    REASON_FINAL_GATE: "final gate",
}


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------


@dataclass
class BoardInjection:
    reason: str
    text: str
    digest: str
    seq: int


@dataclass
class FinalGateDecision:
    action: str  # "none" | "warn" | "continue"
    note: str = ""


@dataclass
class BoardReinjectionState:
    """Per-board bookkeeping for one run.

    ``last_digest``/``seq`` persist for as long as the board does — the dedup has
    to survive a user "continue", or the same render is re-appended every turn.
    The idle counter and the final-gate latches are run-scoped and reset by
    :func:`begin_board_run`.
    """

    board_key: Optional[str] = None
    last_digest: Optional[str] = None
    seq: int = 0
    turns_since_contact: int = 0
    final_gate_continued: bool = False
    final_gate_warned: bool = False


def _material_line(board: Dict[str, Any]) -> str:
    """One compact ``id:resolution/execution[/verdict]`` line per node.

    ``render_board_summary`` only prints a verdict for items already ``resolved``,
    so a verdict landing on a still-open item — one of the material events P4
    must surface — would otherwise be invisible to the hash. This line makes it
    visible to both the model and the dedup, without touching the shared
    renderer the ``/board`` command and the compression handoff use.
    """
    parts = []
    for node in (board or {}).get("nodes", []):
        verdict = (node.get("verification") or {}).get("verdict")
        label = (
            f"{node.get('node_id')}:{node.get('resolution') or 'open'}"
            f"/{node.get('execution') or 'none'}"
        )
        if verdict:
            label += f"/{verdict}"
        parts.append(label)
    return f"  status: {' '.join(parts)}" if parts else ""


def snapshot_body(board: Dict[str, Any]) -> str:
    """The exact board text a snapshot carries (minus the seq/reason header)."""
    from agent.longtask_board import render_board_summary

    return f"{render_board_summary(board)}\n{_material_line(board)}".rstrip()


def snapshot_digest(board: Dict[str, Any]) -> str:
    """Content hash of the *rendered* snapshot.

    Hashing the render (not the board dict) is deliberate: ``updated_at`` and
    other write-time noise must not read as a material change, while a node
    resolving, a verdict landing, or a dependent unlocking must. ``resolution``
    counts, the ready frontier, blocked-by edges, verdicts, and the material line
    below all cover those transitions, so two renders differ exactly when the
    board materially changed.
    """
    return hashlib.sha256(snapshot_body(board).encode("utf-8")).hexdigest()


def unresolved_items(board: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Items the run has not closed out (``open`` or ``in_progress``)."""
    return [
        node
        for node in (board or {}).get("nodes", [])
        if str(node.get("resolution") or "open") in UNRESOLVED_RESOLUTIONS
    ]


def _describe_unresolved(items: List[Dict[str, Any]]) -> str:
    parts = []
    for node in items:
        label = f"{node.get('node_id')} ({node.get('resolution') or 'open'}"
        if node.get("blocked_reason"):
            label += f", blocked: {node.get('blocked_reason')}"
        label += ")"
        goal = str(node.get("goal") or "").strip()
        parts.append(f"{label} {goal[:80]}".rstrip())
    return "; ".join(parts)


def render_snapshot(board: Dict[str, Any], seq: int, reason: str) -> str:
    """Wrap the shared renderer's output as a numbered SNAPSHOT.

    The wording matters as much as the content: once several of these live in the
    transcript, gpt-5.x reads an unlabelled render as current state. "snapshot
    #N, the board file is the source of truth" makes older ones read as history.
    """
    trigger = _TRIGGER_LABELS.get(reason, reason)
    header = (
        f"[long-horizon board] snapshot #{seq} ({trigger}) — rendered now. "
        "The board file is the single source of truth; re-read it with "
        "longtask_read before acting."
    )
    return f"{header}\n{snapshot_body(board)}\n[end board snapshot #{seq}]"


def render_final_gate_note(
    items: List[Dict[str, Any]], *, mode: str, continued: bool
) -> str:
    if mode == "hard" and not continued:
        lead = (
            "[long-horizon board] wrap-up refused: the board still has "
            "unresolved items. Finish them, or resolve/cancel each one with "
            "longtask_update_node (recording why), then answer."
        )
    elif mode == "hard":
        lead = (
            "[long-horizon board] wrap-up allowed (the gate already fired once "
            "this run): the board still has unresolved items — note what remains "
            "open in your answer."
        )
    else:
        lead = (
            "[long-horizon board] this run is wrapping up while the board still "
            "has unresolved items. If the work is genuinely done, say so; "
            "otherwise continue and close the items out."
        )
    return f"{lead}\nUnresolved ({len(items)}): {_describe_unresolved(items)}"


def plan_board_injection(
    board: Optional[Dict[str, Any]],
    state: BoardReinjectionState,
    *,
    board_touched: bool,
    idle_turns: int,
) -> Optional[BoardInjection]:
    """Decide whether to append a snapshot, or ``None`` for a byte-identical no-op.

    The hash check comes first and gates BOTH paths: an unchanged board must
    never grow a tool result, or every turn re-sends an identical render.
    """
    if not board or not board.get("nodes"):
        return None
    digest = snapshot_digest(board)
    if digest == state.last_digest:
        return None
    if board_touched:
        reason = REASON_MATERIAL
    elif state.turns_since_contact >= idle_turns and unresolved_items(board):
        reason = REASON_IDLE
    else:
        return None
    seq = state.seq + 1
    return BoardInjection(
        reason=reason,
        digest=digest,
        seq=seq,
        text=render_snapshot(board, seq, reason),
    )


def record_injection(state: BoardReinjectionState, injection: BoardInjection) -> None:
    state.last_digest = injection.digest
    state.seq = injection.seq
    # An emitted render is fresh board contact: restart the idle countdown so the
    # safety net cannot fire again on the very next turn.
    state.turns_since_contact = 0


def plan_final_gate(
    board: Optional[Dict[str, Any]],
    state: BoardReinjectionState,
    *,
    mode: str,
) -> FinalGateDecision:
    """Decide the wrap-up gate action for a final response with no tool calls.

    "hard" returns "continue" at most once per run (``state.final_gate_continued``);
    after that one extra turn it degrades to "warn" and the model is honoured.
    """
    if not board or mode == "off":
        return FinalGateDecision("none")
    items = unresolved_items(board)
    if not items:
        return FinalGateDecision("none")
    if mode == "hard" and not state.final_gate_continued:
        return FinalGateDecision(
            "continue", render_final_gate_note(items, mode="hard", continued=False)
        )
    return FinalGateDecision(
        "warn",
        render_final_gate_note(
            items, mode=mode, continued=state.final_gate_continued
        ),
    )


def inject_into_newest_tool_result(messages: List[Dict[str, Any]], text: str) -> bool:
    """Grow the newest tool result's content with ``text``. False when there is none.

    The tail message MUST be the tool result: reaching further back would mutate
    a row the model has already answered from, which rewrites past context and
    invalidates the prompt-cache prefix. Nothing is inserted, so strict role
    alternation is untouched (this is the same carrier ``/steer`` uses).
    """
    if not messages:
        return False
    tail = messages[-1]
    if not isinstance(tail, dict) or tail.get("role") != "tool":
        return False
    existing = tail.get("content", "")
    if isinstance(existing, str):
        tail["content"] = existing + "\n\n" + text
    else:
        # Anthropic multimodal content blocks — append a text block.
        try:
            blocks = list(existing) if existing else []
        except TypeError:
            return False
        blocks.append({"type": "text", "text": text})
        tail["content"] = blocks
    return True


def latest_batch_tool_names(messages: List[Dict[str, Any]]) -> Set[str]:
    """Tool names from the newest assistant tool-call batch in ``messages``."""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        calls = msg.get("tool_calls")
        if not calls:
            return set()  # newest assistant turn is a final response
        names: Set[str] = set()
        for call in calls:
            fn = call.get("function") if isinstance(call, dict) else None
            name = (fn or {}).get("name") if isinstance(fn, dict) else None
            if name:
                names.add(str(name))
        return names
    return set()


# ---------------------------------------------------------------------------
# Config knobs (config.yaml only — never an env var for non-secret config)
# ---------------------------------------------------------------------------


def load_longtask_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        longtask = cfg.get("longtask") or {}
        return longtask if isinstance(longtask, dict) else {}
    except Exception:
        return {}


def clamp_idle_turns(value: Any) -> int:
    if isinstance(value, bool):
        return DEFAULT_IDLE_TURNS
    try:
        turns = int(value)
    except (TypeError, ValueError):
        return DEFAULT_IDLE_TURNS
    return max(IDLE_TURNS_FLOOR, min(IDLE_TURNS_CEILING, turns))


def resolve_final_gate_mode(raw: Any) -> str:
    """Normalise ``longtask.enforce_finalization_gate`` to off|warn|hard.

    Booleans are accepted for ergonomics (``true`` → hard, ``false`` → off) so an
    existing ``enforce_finalization_gate: true`` does not silently read as warn.
    """
    if isinstance(raw, bool):
        return "hard" if raw else "off"
    text = str(raw or "").strip().lower()
    if text in FINAL_GATE_MODES:
        return text
    if text in {"true", "yes", "on", "1"}:
        return "hard"
    if text in {"false", "no", "0", "none", "disabled"}:
        return "off"
    return DEFAULT_FINAL_GATE_MODE


def board_idle_turns(config: Optional[Dict[str, Any]] = None) -> int:
    cfg = config if config is not None else load_longtask_config()
    return clamp_idle_turns(cfg.get("board_reinject_idle_turns", DEFAULT_IDLE_TURNS))


def board_final_gate_mode(config: Optional[Dict[str, Any]] = None) -> str:
    cfg = config if config is not None else load_longtask_config()
    return resolve_final_gate_mode(
        cfg.get("enforce_finalization_gate", DEFAULT_FINAL_GATE_MODE)
    )


# ---------------------------------------------------------------------------
# Agent-facing wiring
# ---------------------------------------------------------------------------


def _load_board_for_agent(agent: Any) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Resolve the acting session's board, or ``(None, None)``.

    Reads through the SAME resolver the tool handlers and the compression engine
    use (``tools/longtask_tool._root`` / ``._session_id``) so the agent, the
    tools, and the handoff can never disagree about which board this run is on:
    one run, one board — no per-agent shadow board. A delegated child never gets
    one (the board is the parent's planning state).
    """
    try:
        from agent.delegation_context import is_delegated_child_context

        if is_delegated_child_context():
            return None, None
    except Exception:
        pass
    try:
        from agent.longtask_board import board_path, load_board
        from tools.longtask_tool import _root, _session_id

        root = _root(agent)
        sid = _session_id(agent)
        path = board_path(root, sid)
        if not path.exists():
            return None, None
        return str(path), load_board(root, sid)
    except Exception:
        return None, None


def _state_for(agent: Any, key: Optional[str]) -> BoardReinjectionState:
    state = getattr(agent, "_board_reinjection_state", None)
    if not isinstance(state, BoardReinjectionState) or state.board_key != key:
        # A different board (new task contract / new workspace) starts a fresh
        # dedup + sequence; carrying the old digest over would suppress the first
        # render of the new board as "unchanged".
        state = BoardReinjectionState(board_key=key)
        try:
            agent._board_reinjection_state = state
        except Exception:
            pass
    return state


def begin_board_run(agent: Any) -> None:
    """Reset the run-scoped board counters. Called once per conversation turn."""
    state = getattr(agent, "_board_reinjection_state", None)
    if isinstance(state, BoardReinjectionState):
        state.turns_since_contact = 0
        state.final_gate_continued = False
        state.final_gate_warned = False


def _invalidate_persisted_tail(agent: Any, messages: List[Dict[str, Any]]) -> None:
    """Force the next DB flush to rewrite the tool row we just grew.

    CONTRACT (#92231, run_agent.py): a dict stamped ``_db_persisted`` asserts its
    content is durable as written; mutating it in place without popping the
    marker leaves state.db stale, so the model never sees the snapshot when the
    session resumes. The bounded flush-scan prefix is invalidated for the same
    reason (the mutating dict sits inside it).
    """
    try:
        from agent.context_compressor import _DB_PERSISTED_MARKER as marker
    except Exception:  # pragma: no cover - import guard
        marker = "_db_persisted"
    try:
        if messages and isinstance(messages[-1], dict):
            messages[-1].pop(marker, None)
    except Exception:
        pass
    try:
        agent._db_flush_scan_prefix = None
    except Exception:
        pass


def maybe_reinject_board(agent: Any, messages: List[Dict[str, Any]]) -> bool:
    """Append a board snapshot to the newest tool result when warranted.

    Returns True only when a snapshot was actually appended. Suppressed (False)
    when there is no board, no fresh tool result to ride, the board has not
    materially changed since the last render, or the idle safety net has not come
    due — in every one of those cases the tool result is left byte-identical.
    """
    if not messages:
        return False
    tail = messages[-1]
    if not isinstance(tail, dict) or tail.get("role") != "tool":
        return False  # first iteration / no tool output to piggyback on
    key, board = _load_board_for_agent(agent)
    if board is None:
        return False
    state = _state_for(agent, key)
    board_touched = bool(BOARD_CONTACT_TOOL_NAMES & latest_batch_tool_names(messages))
    if board_touched:
        state.turns_since_contact = 0
    else:
        state.turns_since_contact += 1
    injection = plan_board_injection(
        board,
        state,
        board_touched=board_touched,
        idle_turns=board_idle_turns(),
    )
    if injection is None:
        return False
    if not inject_into_newest_tool_result(messages, injection.text):
        return False
    record_injection(state, injection)
    _invalidate_persisted_tail(agent, messages)
    logger.info(
        "Longtask board snapshot injected (reason=%s seq=%d session=%s)",
        injection.reason,
        injection.seq,
        getattr(agent, "session_id", None) or "none",
    )
    return True


def apply_final_gate(
    agent: Any,
    messages: List[Dict[str, Any]],
    final_response: Optional[str],
    *,
    finish_reason: Optional[str] = None,
) -> Tuple[Optional[str], bool]:
    """Wrap-up gate for a final response with no tool calls.

    Returns ``(final_response, continue_turn)``. A True second element asks the
    caller to take exactly ONE more loop iteration; the note has already been
    appended to the newest tool result, so no user message is needed (and none is
    created). The run-scoped latch in :func:`plan_final_gate` makes a second
    "continue" impossible — never a loop.
    """
    if finish_reason == "tool_calls":
        # Not a genuine wrap-up — the dropped-tool-call recovery owns this.
        return final_response, False
    if not final_response or not str(final_response).strip():
        return final_response, False
    mode = board_final_gate_mode()
    if mode == "off":
        return final_response, False
    key, board = _load_board_for_agent(agent)
    if board is None:
        return final_response, False
    state = _state_for(agent, key)
    decision = plan_final_gate(board, state, mode=mode)
    if decision.action == "none":
        return final_response, False
    note = decision.note
    if not inject_into_newest_tool_result(messages, note):
        # No tool result to carry the note (a wrap-up with no tools this turn).
        # Fall back to the visible response rather than inventing a carrier — a
        # mid-loop user message is forbidden.
        logger.info(
            "Longtask final gate: unresolved items appended to the response "
            "(action=%s, no tool result to carry session=%s)",
            decision.action,
            getattr(agent, "session_id", None) or "none",
        )
        return f"{final_response}\n\n{note}", False
    if decision.action == "continue":
        state.final_gate_continued = True
        _invalidate_persisted_tail(agent, messages)
        logger.info(
            "Longtask final gate: wrap-up refused once (mode=hard unresolved=%d "
            "session=%s)",
            len(unresolved_items(board)),
            getattr(agent, "session_id", None) or "none",
        )
        return final_response, True
    state.final_gate_warned = True
    logger.info(
        "Longtask final gate: unresolved items surfaced at wrap-up (mode=%s "
        "unresolved=%d session=%s)",
        mode,
        len(unresolved_items(board)),
        getattr(agent, "session_id", None) or "none",
    )
    return final_response, False


__all__ = [
    "BOARD_CONTACT_TOOL_NAMES",
    "DEFAULT_FINAL_GATE_MODE",
    "DEFAULT_IDLE_TURNS",
    "BoardInjection",
    "BoardReinjectionState",
    "FinalGateDecision",
    "REASON_FINAL_GATE",
    "REASON_IDLE",
    "REASON_MATERIAL",
    "apply_final_gate",
    "begin_board_run",
    "board_final_gate_mode",
    "board_idle_turns",
    "clamp_idle_turns",
    "inject_into_newest_tool_result",
    "latest_batch_tool_names",
    "maybe_reinject_board",
    "plan_board_injection",
    "plan_final_gate",
    "record_injection",
    "render_final_gate_note",
    "render_snapshot",
    "resolve_final_gate_mode",
    "snapshot_body",
    "snapshot_digest",
    "unresolved_items",
]
