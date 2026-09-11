"""Long-horizon context engine.

This engine replaces the default LLM summarizing compressor with a
checkpoint-first handoff: old transcript is archived by a memory provider, then
the live prompt is rebuilt around a compact manifest and recent tail.
"""

from __future__ import annotations

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
        if archived:
            lines.extend(["", "Archive manifest:", archived])
        else:
            lines.extend(["", "Archive manifest: no memory provider checkpoint text was returned."])
        return {"role": "assistant", "content": "\n".join(lines)}

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
