"""Long-horizon context engine.

This engine replaces the default LLM summarizing compressor with a
checkpoint-first handoff: old transcript is archived by a memory provider, then
the live prompt is rebuilt around a compact manifest and recent tail.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.context_engine import ContextEngine, sanitize_memory_context


class LongHorizonContextEngine(ContextEngine):
    """Conservative compression engine for durable long-running tasks."""

    threshold_percent = 0.75
    protect_first_n = 1
    protect_last_n = 10
    emit_automatic_compaction_status = True

    def __init__(self) -> None:
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.threshold_tokens = 0
        self.context_length = 0
        self.compression_count = 0
        self._session_id = ""
        self._platform = "cli"
        self._model = ""
        self._hermes_home = ""

    @property
    def name(self) -> str:
        return "long_horizon"

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        self.last_prompt_tokens = int(
            usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        )
        self.last_completion_tokens = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        self.last_total_tokens = int(usage.get("total_tokens") or (
            self.last_prompt_tokens + self.last_completion_tokens
        ))

    def should_compress(self, prompt_tokens: int = None) -> bool:
        tokens = (
            self.last_prompt_tokens
            if prompt_tokens is None
            else int(prompt_tokens or 0)
        )
        return bool(self.threshold_tokens and tokens >= self.threshold_tokens)

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
        force: bool = False,
        memory_context: str = "",
    ) -> List[Dict[str, Any]]:
        if len(messages) <= self.protect_first_n + self.protect_last_n + 1:
            return messages

        self.compression_count += 1
        head = self._protected_head(messages)
        tail = self._safe_tail(messages)
        handoff = self._handoff_message(
            original_count=len(messages),
            retained_count=len(head) + len(tail),
            current_tokens=current_tokens,
            focus_topic=focus_topic,
            memory_context=memory_context,
        )
        compacted = []
        for message in head + [handoff] + tail:
            self._append_valid(compacted, message)
        return compacted

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        super().on_session_start(session_id, **kwargs)
        self._session_id = session_id or ""
        self._platform = str(kwargs.get("platform") or "cli")
        self._model = str(kwargs.get("model") or self._model)
        self._hermes_home = str(kwargs.get("hermes_home") or self._hermes_home)

    def get_automatic_compaction_status_message(self, **kwargs: Any) -> str:
        return "Archiving context and compacting long-horizon state..."

    def _protected_head(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        protected = []
        non_system = 0
        for message in messages:
            role = message.get("role")
            if role == "system":
                protected.append(dict(message))
                continue
            if non_system < self.protect_first_n:
                protected.append(dict(message))
                non_system += 1
                continue
            break
        return protected

    def _safe_tail(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        start = max(0, len(messages) - self.protect_last_n)
        while start < len(messages) and messages[start].get("role") == "tool":
            start += 1
        tail = [dict(message) for message in messages[start:]]
        if tail and tail[0].get("role") == "assistant" and tail[0].get("tool_calls"):
            return tail
        return tail

    def _handoff_message(
        self,
        *,
        original_count: int,
        retained_count: int,
        current_tokens: Optional[int],
        focus_topic: Optional[str],
        memory_context: str,
    ) -> Dict[str, Any]:
        archived = sanitize_memory_context(memory_context or "")
        lines = [
            "Long-horizon compression handoff.",
            f"Session: {self._session_id or 'unknown'}",
            f"Original messages: {original_count}",
            f"Retained live messages: {retained_count}",
        ]
        if current_tokens:
            lines.append(f"Approx tokens before compression: {current_tokens}")
        if focus_topic:
            lines.append(f"Compression focus: {focus_topic}")
        lines.extend([
            "",
            "Operational contract:",
            "- Treat the recent tail as authoritative for current state.",
            "- Use task board files for multi-agent scheduling state when present.",
            "- Use session_archive_search and session_archive_expand when older raw evidence is needed.",
            "- Prefer claim-evidence reports over unsupported recall.",
        ])
        board = self._task_board_handoff()
        if board:
            lines.extend(["", "Task board state:", board])
        if archived:
            lines.extend(["", "Archive manifest:", archived])
        else:
            lines.extend(["", "Archive manifest: no memory provider checkpoint text was returned."])
        return {"role": "assistant", "content": "\n".join(lines)}

    def _task_board_handoff(self) -> str:
        board = self._load_task_board()
        if not board:
            return ""
        nodes = board.get("nodes") if isinstance(board.get("nodes"), list) else []
        global_state = (
            board.get("global_state")
            if isinstance(board.get("global_state"), dict)
            else {}
        )
        counts: Dict[str, int] = {}
        for node in nodes:
            if not isinstance(node, dict):
                continue
            status = str(node.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1

        ready = self._node_lines(
            [n for n in nodes if self._node_status(n) in {"ready", "pending"}],
            limit=6,
            require_ready=True,
            all_nodes=nodes,
        )
        running = self._node_lines(
            [n for n in nodes if self._node_status(n) == "running"],
            limit=4,
        )
        blocked = self._node_lines(
            [n for n in nodes if self._node_status(n) == "blocked"],
            limit=4,
        )
        terminal = self._node_lines(
            [n for n in nodes if self._node_status(n) in {"done", "failed", "timeout"}],
            limit=6,
            include_verification=True,
        )

        lines = [
            f"task_id: {board.get('task_id') or 'unknown'}",
            f"objective: {str(board.get('objective') or '').strip()[:700]}",
            "status_counts: "
            + ", ".join(f"{key}={counts[key]}" for key in sorted(counts)),
        ]
        for key in ("decisions", "constraints", "open_questions"):
            values = global_state.get(key) if isinstance(global_state, dict) else []
            rendered = self._bounded_list(values, limit=6)
            if rendered:
                lines.append(f"{key}:")
                lines.extend(f"- {item}" for item in rendered)
        if ready:
            lines.append("ready_frontier:")
            lines.extend(ready)
        if running:
            lines.append("running:")
            lines.extend(running)
        if blocked:
            lines.append("blocked:")
            lines.extend(blocked)
        if terminal:
            lines.append("recent_terminal:")
            lines.extend(terminal)
        return "\n".join(lines)[:6000]

    def _load_task_board(self) -> Dict[str, Any]:
        if not self._session_id:
            return {}
        try:
            from agent.longtask_board import load_board
        except Exception:
            return {}
        for root in self._task_board_roots():
            try:
                return load_board(root, self._session_id)
            except Exception:
                continue
        return {}

    def _task_board_roots(self) -> List[Path]:
        """Candidate board roots, in writer-compatible order.

        ``tools/longtask_tool.py::_root`` writes the board under the PARENT
        AGENT's workspace — ``terminal_cwd`` / agent cwd / subdirectory hints,
        falling back to the process cwd. Probing only ``Path.cwd()`` therefore
        silently missed the board whenever the two differ: verified on a
        gateway-shaped session (board present under ``terminal.cwd``, process cwd
        elsewhere) where the handoff rendered no board state at all — the one
        thing this engine exists to carry. The engine has no agent object, so
        mirror the writer's env-visible head (``TERMINAL_CWD``, which the gateway
        bridges from ``terminal.cwd``) and keep ``HERMES_HOME/tasks`` last.
        """
        candidates: List[str] = []
        env_cwd = os.environ.get("TERMINAL_CWD")
        if env_cwd:
            candidates.append(env_cwd)
        try:
            candidates.append(os.getcwd())
        except OSError:
            pass
        roots: List[Path] = []
        seen = set()
        for value in candidates:
            root = Path(value).expanduser() / ".hermes" / "tasks"
            if str(root) not in seen:
                seen.add(str(root))
                roots.append(root)
        if self._hermes_home:
            root = Path(self._hermes_home) / "tasks"
            if str(root) not in seen:
                roots.append(root)
        return roots

    def _node_status(self, node: Any) -> str:
        return str(node.get("status") or "unknown") if isinstance(node, dict) else "unknown"

    def _node_lines(
        self,
        nodes: List[Dict[str, Any]],
        *,
        limit: int,
        require_ready: bool = False,
        all_nodes: List[Dict[str, Any]] | None = None,
        include_verification: bool = False,
    ) -> List[str]:
        out = []
        for node in nodes:
            if require_ready and not self._dependencies_done(node, all_nodes or []):
                continue
            goal = str(node.get("goal") or "").strip().replace("\n", " ")[:280]
            deps = ",".join(str(dep) for dep in node.get("dependencies") or [])
            line = (
                f"- {node.get('node_id')} status={node.get('status')} "
                f"deps=[{deps}] goal={goal}"
            )
            if include_verification and isinstance(node.get("verification"), dict):
                verification = node["verification"]
                verdict = verification.get("verdict") or "unknown"
                summary = str(verification.get("summary_for_parent") or "").strip()
                line += f" verification={verdict}"
                if summary:
                    line += f" summary={summary[:300]}"
            claims = node.get("claims") if isinstance(node.get("claims"), list) else []
            if claims:
                line += f" claims={self._compact_json(claims[:2], 500)}"
            if node.get("report_path"):
                line += f" report={node.get('report_path')}"
            out.append(line[:1000])
            if len(out) >= limit:
                break
        return out

    def _dependencies_done(
        self,
        node: Dict[str, Any],
        all_nodes: List[Dict[str, Any]],
    ) -> bool:
        deps = set(str(dep) for dep in node.get("dependencies") or [])
        if not deps:
            return True
        status_by_id = {
            str(item.get("node_id")): str(item.get("status") or "")
            for item in all_nodes
            if isinstance(item, dict)
        }
        return all(status_by_id.get(dep) == "done" for dep in deps)

    def _bounded_list(self, values: Any, *, limit: int) -> List[str]:
        if not isinstance(values, list):
            return []
        out = []
        for value in values:
            if isinstance(value, dict):
                text = self._compact_json(value, 500)
            else:
                text = str(value)
            text = text.strip().replace("\n", " ")[:500]
            if text:
                out.append(text)
            if len(out) >= limit:
                break
        return out

    def _compact_json(self, value: Any, max_chars: int) -> str:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            text = str(value)
        return text[:max_chars]

    def _append_valid(self, out: List[Dict[str, Any]], message: Dict[str, Any]) -> None:
        role = message.get("role")
        if not out:
            out.append(message)
            return
        prev_role = out[-1].get("role")
        if role == prev_role and role in {"user", "assistant"}:
            merged = dict(out[-1])
            merged["content"] = (
                f"{merged.get('content') or ''}\n\n{message.get('content') or ''}"
            ).strip()
            if message.get("tool_calls") and not merged.get("tool_calls"):
                merged["tool_calls"] = message.get("tool_calls")
            out[-1] = merged
            return
        out.append(message)


def register(ctx) -> None:
    ctx.register_context_engine(LongHorizonContextEngine())
