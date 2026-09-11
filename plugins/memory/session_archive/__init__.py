"""Local session archive memory provider.

Stores compression checkpoints under HERMES_HOME so compaction can replace
large transcripts without making old tool evidence permanently unreachable.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider, PRE_COMPRESS_CHECKPOINT_API_VERSION
from agent.redact import redact_sensitive_text
from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)


SEARCH_SCHEMA = {
    "name": "session_archive_search",
    "description": "Search archived pre-compression transcript chunks for this session. Returns bounded previews and chunk ids.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text to search for."},
            "limit": {"type": "integer", "description": "Maximum results, default 5, max 20."},
        },
        "required": ["query"],
    },
}

EXPAND_SCHEMA = {
    "name": "session_archive_expand",
    "description": "Expand one archived transcript chunk by id when raw pre-compression evidence is needed.",
    "parameters": {
        "type": "object",
        "properties": {
            "chunk_id": {"type": "string", "description": "Chunk id returned by session_archive_search or the compression manifest."},
            "max_chars": {"type": "integer", "description": "Maximum returned characters, default 12000, max 40000."},
        },
        "required": ["chunk_id"],
    },
}

CLAIMS_SCHEMA = {
    "name": "session_archive_get_claims",
    "description": "List claim-evidence reports archived from delegated subagent work in this session.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Maximum reports, default 10, max 50."},
        },
        "required": [],
    },
}


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", (value or "session").strip())
    return cleaned[:96] or "session"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def _message_text(message: Dict[str, Any]) -> str:
    parts = [str(message.get("role") or "unknown")]
    name = message.get("name") or message.get("tool_call_id")
    if name:
        parts.append(str(name))
    if message.get("tool_calls"):
        parts.append(_as_text(message.get("tool_calls")))
    parts.append(_as_text(message.get("content")))
    if message.get("reasoning"):
        parts.append(_as_text(message.get("reasoning")))
    return "\n".join(part for part in parts if part)


_RECAP_PREFIX = "Deterministic recap ("


def _starts_new_turn(message: Dict[str, Any]) -> bool:
    """Whether ``message`` opens a user turn — the preferred chunk boundary."""
    return isinstance(message, dict) and str(message.get("role") or "") == "user"


def _message_hash(message: Dict[str, Any]) -> str:
    """Content address for one transcript message.

    The archive decides what still needs summarizing by *message*, not by chunk
    id. Chunk boundaries legitimately move when a compression handoff replaces
    the transcript's head, and a position-keyed decision then re-summarizes
    material that is already on disk.
    """
    if not isinstance(message, dict):
        payload = repr(message)
    else:
        payload = f"{message.get('role') or ''}\0{_message_text(message)}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


class SessionArchiveProvider(MemoryProvider):
    """Durable local archive used by checkpoint-aware compression."""

    pre_compress_checkpoint_api_version = PRE_COMPRESS_CHECKPOINT_API_VERSION
    max_messages_per_chunk = 20
    max_chars_per_chunk = 30_000
    secondary_index_chunk_threshold = 24
    secondary_index_group_size = 12
    # LLM summaries are ON THE CRITICAL PATH of a compression pass, which the host
    # bounds by inactivity (compression.context_timeout_seconds, default 120s):
    # exceed it and the pass is abandoned — "Context compression made no
    # progress" — leaving the context over the provider limit. A 700-message
    # transcript is ~36 chunks; summarising each one sequentially (30s timeout,
    # plus the client's own retry) blew straight through that budget. Chunks are
    # always written; only the progressive-disclosure summaries are rationed,
    # newest first (recency is what retrieval actually hits).
    #
    # Budgets are an ABORT VALVE, not the normal path (user ruling, 2026-09-12:
    # in-path summarisation is the design — a compression pass IS a context
    # rebuild, so summary quality outranks pass latency). They stop a hung
    # provider, and they sit just under the host's own inactivity window
    # (compression.context_timeout_seconds, default 120s) so the archive degrades
    # to deterministic recaps on its own terms instead of having the whole pass
    # aborted mid-flight. Concurrency is what makes the full set affordable:
    # 36 chunks × ~8s serially exceeds that window on its own.
    max_llm_summaries_per_pass = 64
    llm_summary_budget_seconds = 120.0
    llm_summary_concurrency = 4

    def __init__(self) -> None:
        self._session_id = ""
        self._root: Path | None = None
        self._config: Dict[str, Any] = {}
        self._llm_summary_calls = 0
        self._llm_summary_deadline: Optional[float] = None
        self._llm_summary_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "session_archive"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        hermes_home = Path(str(kwargs.get("hermes_home") or get_hermes_home()))
        self._root = hermes_home / "session_archive"
        self._session_id = _safe_id(session_id)
        self._config = self._load_config()
        self.max_messages_per_chunk = _bounded_int(
            self._config.get("max_messages_per_chunk"),
            self.max_messages_per_chunk,
            1,
            100,
        )
        self.max_chars_per_chunk = _bounded_int(
            self._config.get("max_chars_per_chunk"),
            self.max_chars_per_chunk,
            2000,
            200_000,
        )
        self.secondary_index_chunk_threshold = _bounded_int(
            self._config.get("secondary_index_chunk_threshold"),
            self.secondary_index_chunk_threshold,
            2,
            1000,
        )
        self.secondary_index_group_size = _bounded_int(
            self._config.get("secondary_index_group_size"),
            self.secondary_index_group_size,
            2,
            100,
        )
        self.max_llm_summaries_per_pass = _bounded_int(
            self._config.get("max_llm_summaries_per_pass"),
            self.max_llm_summaries_per_pass,
            0,
            64,
        )
        self.llm_summary_budget_seconds = float(
            _bounded_int(
                self._config.get("llm_summary_budget_seconds"),
                int(self.llm_summary_budget_seconds),
                0,
                600,
            )
        )
        self.llm_summary_concurrency = _bounded_int(
            self._config.get("llm_summary_concurrency"),
            self.llm_summary_concurrency,
            1,
            16,
        )
        self._session_dir().mkdir(parents=True, exist_ok=True)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        self._session_id = _safe_id(new_session_id)
        if self._root is not None:
            self._session_dir().mkdir(parents=True, exist_ok=True)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, EXPAND_SCHEMA, CLAIMS_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        if tool_name == "session_archive_search":
            return json.dumps(
                self.search(str(args.get("query") or ""), limit=_bounded_int(args.get("limit"), 5, 1, 20)),
                ensure_ascii=False,
            )
        if tool_name == "session_archive_expand":
            return json.dumps(
                self.expand(
                    str(args.get("chunk_id") or ""),
                    max_chars=_bounded_int(args.get("max_chars"), 12_000, 500, 40_000),
                ),
                ensure_ascii=False,
            )
        if tool_name in {"session_archive_get_claims", "session_archive_claims"}:
            return json.dumps(
                self.claims(limit=_bounded_int(args.get("limit"), 10, 1, 50)),
                ensure_ascii=False,
            )
        return json.dumps({"error": f"Unknown session archive tool: {tool_name}"})

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        chunks = self._write_checkpoint(messages, source="pre_compress")
        index = self._read_index()
        if not chunks and not index:
            return ""
        # A pass over already-archived material writes nothing new, but the next
        # turn still needs the archive pointer — fall back to the newest entries
        # so the checkpoint marker never disappears from the context.
        listed = chunks or index[-12:]
        lines = [
            "[SESSION ARCHIVE CHECKPOINT]",
            f"provider: {self.name}",
            f"session_id: {self._session_id}",
            f"archived_chunks: {len(index)}",
            f"new_chunks_this_pass: {len(chunks)}",
            "Use session_archive_search(query) and session_archive_expand(chunk_id) to recover raw pre-compression context.",
        ]
        for chunk in listed[:12]:
            summary = chunk.get("summary") or chunk.get("preview") or ""
            lines.append(
                f"- {chunk.get('chunk_id')} messages={chunk.get('message_start')}-{chunk.get('message_end')} summary={summary}"
            )
        if len(listed) > 12:
            lines.append(f"- ... {len(listed) - 12} more chunks archived")
        secondary = self._read_secondary_index()
        if secondary:
            lines.append(
                f"secondary_index: {len(secondary)} recap group(s) available in archive"
            )
            for group in secondary[:6]:
                chunk_ids = [
                    str(chunk_id)
                    for chunk_id in (group.get("chunk_ids") or [])
                    if chunk_id
                ]
                lines.append(
                    f"- {group.get('group_id')} chunks={','.join(chunk_ids)} "
                    f"summary={str(group.get('summary') or '')[:700]}"
                )
        return "\n".join(lines)

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        payload = {
            "kind": "delegation_report",
            "created_at": time.time(),
            "parent_session_id": self._session_id,
            "child_session_id": child_session_id,
            "task": task,
            "result": result,
            "metadata": dict(kwargs or {}),
        }
        reports = self._read_reports()
        report_id = hashlib.sha256(
            f"{self._session_id}\0{child_session_id}\0{task}\0{result}".encode("utf-8", "replace")
        ).hexdigest()[:16]
        payload["report_id"] = report_id
        reports = [r for r in reports if r.get("report_id") != report_id]
        reports.append(payload)
        atomic_json_write(self._reports_path(), reports, indent=2, mode=0o600)

    def search(self, query: str, *, limit: int = 5) -> Dict[str, Any]:
        needle = query.casefold().strip()
        if not needle:
            return {"results": []}
        results = []
        for item in reversed(self._read_index()):
            haystack = (item.get("search_text") or "").casefold()
            if needle not in haystack:
                continue
            results.append({
                "chunk_id": item.get("chunk_id"),
                "message_start": item.get("message_start"),
                "message_end": item.get("message_end"),
                "created_at": item.get("created_at"),
                "summary": redact_sensitive_text(str(item.get("summary") or ""), force=True),
                "preview": redact_sensitive_text(str(item.get("preview") or ""), force=True),
            })
            if len(results) >= limit:
                break
        return {"results": results}

    def expand(self, chunk_id: str, *, max_chars: int = 12_000) -> Dict[str, Any]:
        path = self._chunks_dir() / f"{_safe_id(chunk_id)}.json"
        if not path.exists():
            return {"error": f"Archive chunk not found: {chunk_id}"}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"error": f"Archive chunk unreadable: {type(exc).__name__}"}
        messages = data.get("messages") if isinstance(data, dict) else []
        text = "\n\n".join(_message_text(m) for m in messages if isinstance(m, dict))
        redacted = redact_sensitive_text(text, force=True, redact_url_credentials=True)
        truncated = len(redacted) > max_chars
        if truncated:
            redacted = redacted[:max_chars] + "\n...[archive chunk truncated]..."
        return {
            "chunk_id": data.get("chunk_id", chunk_id),
            "message_start": data.get("message_start"),
            "message_end": data.get("message_end"),
            "summary": data.get("summary") or "",
            "truncated": truncated,
            "content": redacted,
        }

    def claims(self, *, limit: int = 10) -> Dict[str, Any]:
        reports = list(reversed(self._read_reports()))[:limit]
        out = []
        for report in reports:
            out.append({
                "report_id": report.get("report_id"),
                "child_session_id": report.get("child_session_id"),
                "task": redact_sensitive_text(str(report.get("task") or ""), force=True),
                "result_preview": redact_sensitive_text(str(report.get("result") or "")[:1500], force=True),
                "created_at": report.get("created_at"),
            })
        return {"reports": out}

    def backup_paths(self) -> List[str]:
        return []

    def _message_chunks(
        self, messages: List[Dict[str, Any]]
    ) -> List[tuple[int, int, List[Dict[str, Any]]]]:
        """Split a transcript into ``(start, end, messages)`` groups. No IO, no LLM.

        Boundaries are content-driven, never index-driven: the split depends only
        on the message sequence, and once the soft limits are reached a chunk is
        only closed where a *user turn* starts (or where a hard limit forces it).
        So the same messages keep landing in the same chunk when the transcript's
        head is replaced by a compression handoff, and a chunk stops ending
        mid-turn when the next user message is close by. Hard limits (1.5x
        messages, 1.25x chars) bound how far a split will wait for a turn.
        """
        soft_messages = max(1, int(self.max_messages_per_chunk))
        hard_messages = int(soft_messages * 1.5) + 1
        soft_chars = max(1, int(self.max_chars_per_chunk))
        hard_chars = int(soft_chars * 1.25) + 1

        groups: List[tuple[int, int, List[Dict[str, Any]]]] = []
        current: List[Dict[str, Any]] = []
        current_chars = 0
        start = 0
        for idx, message in enumerate(messages):
            text_len = len(_message_text(message))
            if current:
                over_soft = len(current) >= soft_messages or current_chars >= soft_chars
                over_hard = (
                    len(current) >= hard_messages
                    or current_chars + text_len > hard_chars
                )
                if over_hard or (over_soft and _starts_new_turn(message)):
                    groups.append((start, idx - 1, current))
                    current = []
                    current_chars = 0
                    start = idx
            current.append(dict(message))
            current_chars += text_len
        if current:
            groups.append((start, len(messages) - 1, current))
        return groups

    def _chunk_id_for(
        self, start: int, end: int, messages: List[Dict[str, Any]]
    ) -> str:
        joined = "\n\n".join(_message_text(m) for m in messages)
        digest = hashlib.sha256(
            f"{self._session_id}\0{start}\0{end}\0{joined}".encode("utf-8", "replace")
        ).hexdigest()[:16]
        return f"chk-{start}-{end}-{digest}"

    def _llm_summary_budget_exhausted(self) -> bool:
        """True once this pass has spent its summary allowance.

        Both caps matter: the count bounds a fast provider, the deadline bounds a
        slow one (including the auxiliary client's own transient retry).
        """
        if getattr(self, "_llm_summary_calls", 0) >= self.max_llm_summaries_per_pass:
            return True
        deadline = getattr(self, "_llm_summary_deadline", None)
        return deadline is not None and time.monotonic() >= deadline

    def _seen_cache_path(self) -> Path:
        return self._session_dir() / "seen.json"

    def _archived_message_hashes(self) -> set:
        """Message hashes the archive already holds, derived from its chunks.

        ``seen.json`` caches the per-chunk hash lists so a pass only reads chunks
        it has not hashed before; a chunk that moved or vanished falls back to a
        fresh read, which keeps the answer honest if the archive is pruned.
        """
        cache = self._read_json(self._seen_cache_path())
        if not isinstance(cache, dict):
            cache = {}
        archived: set = set()
        refreshed: Dict[str, List[str]] = {}
        changed = False
        for entry in self._read_index():
            chunk_id = str(entry.get("chunk_id") or "")
            if not chunk_id:
                continue
            path = self._chunks_dir() / f"{_safe_id(chunk_id)}.json"
            cached = cache.get(chunk_id)
            if isinstance(cached, list) and path.exists():
                hashes = [str(item) for item in cached]
            else:
                payload = self._read_json(path)
                messages = payload.get("messages") if isinstance(payload, dict) else None
                hashes = (
                    [_message_hash(m) for m in messages if isinstance(m, dict)]
                    if isinstance(messages, list)
                    else []
                )
                changed = True
            refreshed[chunk_id] = hashes
            archived.update(hashes)
        if len(refreshed) != len(cache):
            changed = True
        if changed:
            try:
                atomic_json_write(self._seen_cache_path(), refreshed, indent=2, mode=0o600)
            except Exception:
                logger.debug("session_archive: could not persist seen.json", exc_info=True)
        return archived

    def _unseen_runs(
        self, messages: List[Dict[str, Any]], archived: set
    ) -> List[tuple[int, int, List[Dict[str, Any]]]]:
        """Contiguous runs of messages the archive has not stored yet."""
        runs: List[tuple[int, int, List[Dict[str, Any]]]] = []
        current: List[Dict[str, Any]] = []
        start = 0
        for idx, message in enumerate(messages):
            if _message_hash(message) in archived:
                if current:
                    runs.append((start, idx - 1, current))
                    current = []
                start = idx + 1
                continue
            if not current:
                start = idx
            current.append(dict(message))
        if current:
            runs.append((start, len(messages) - 1, current))
        return runs

    def _upgrade_recap_chunks(self) -> int:
        """Re-summarize chunks that only carry a deterministic recap.

        The recap is the cheap baseline that always lands on disk; the LLM
        summary is what upgrades it. Whatever the pass budget did not cover stays
        marked ``summary_kind: recap`` in the index, so a later pass picks up
        where this one stopped — the summary list fills in incrementally instead
        of being recomputed, and the recap stage (below) then aggregates the
        upgraded text.
        """
        if not self._summary_config().get("enabled"):
            return 0
        pending = [
            entry
            for entry in self._read_index()
            if str(entry.get("summary_kind") or "llm") == "recap" and entry.get("chunk_id")
        ]
        if not pending:
            return 0
        upgraded: List[Dict[str, Any]] = []
        for entry in reversed(pending):  # newest first — recency is what retrieval hits
            if self._llm_summary_budget_exhausted():
                break
            chunk_id = str(entry["chunk_id"])
            path = self._chunks_dir() / f"{_safe_id(chunk_id)}.json"
            payload = self._read_json(path)
            if not isinstance(payload, dict):
                continue
            messages = [
                m for m in (payload.get("messages") or []) if isinstance(m, dict)
            ]
            if not messages:
                continue
            text = "\n\n".join(_message_text(m) for m in messages)
            summary = self._summarize_chunk(text, messages=messages)
            if summary.startswith(_RECAP_PREFIX):
                continue  # still no LLM result: leave it for a later pass
            payload["summary"] = summary
            payload["summary_kind"] = "llm"
            atomic_json_write(path, payload, indent=2, mode=0o600)
            upgraded.append(
                self._chunk_entry(
                    chunk_id,
                    entry.get("message_start"),
                    entry.get("message_end"),
                    summary,
                    messages,
                    entry.get("created_at"),
                    "llm",
                )
            )
        if upgraded:
            self._merge_index(upgraded)
        return len(upgraded)

    def _write_checkpoint(self, messages: List[Dict[str, Any]], *, source: str) -> List[Dict[str, Any]]:
        # Only material the archive has never seen is chunked. Every compression
        # re-hands the whole transcript, so without this a pass re-summarized the
        # same messages on each attempt, and a handoff replacing the head pushed
        # the *entire* transcript through the summarizer again (measured: a
        # 5-message head shift re-summarized 100% of a 4-chunk archive).
        archived = self._archived_message_hashes()
        groups: List[tuple[int, int, List[Dict[str, Any]]]] = []
        for run_start, _run_end, run in self._unseen_runs(messages, archived):
            groups.extend(
                (start + run_start, end + run_start, group)
                for start, end, group in self._message_chunks(run)
            )

        # Decide which chunks may spend an LLM call BEFORE writing anything:
        # every group here holds unseen messages, but only the newest few get an
        # LLM summary in this pass. Everything else is written with a
        # deterministic recap, so the pass stays bounded no matter how long the
        # transcript is — and the leftover recaps are upgraded by later passes.
        allow_llm: set = set()
        if self._summary_config().get("enabled") and self.max_llm_summaries_per_pass > 0:
            fresh = [self._chunk_id_for(start, end, group) for start, end, group in groups]
            allow_llm = set(list(reversed(fresh))[: self.max_llm_summaries_per_pass])

        self._llm_summary_calls = 0
        self._llm_summary_deadline = time.monotonic() + self.llm_summary_budget_seconds

        prepared = [
            self._prepare_chunk(
                group,
                start,
                end,
                source=source,
                allow_llm=self._chunk_id_for(start, end, group) in allow_llm,
            )
            for start, end, group in groups
        ]
        summaries = self._summarize_chunks_concurrently(prepared)
        chunks = [
            self._write_prepared_chunk(item, summary)
            for item, summary in zip(prepared, summaries)
        ]
        # Recaps written above are the trigger for their own upgrade: whatever the
        # budget did not cover is picked up by the next pass.
        self._upgrade_recap_chunks()
        self._merge_index(chunks)
        self._maybe_write_secondary_index()
        return chunks

    def _prepare_chunk(
        self,
        messages: List[Dict[str, Any]],
        start: int,
        end: int,
        *,
        source: str,
        allow_llm: bool = True,
    ) -> Dict[str, Any]:
        """Deterministic per-chunk setup: no LLM call, no write.

        Split out of :meth:`_store_chunk` so the LLM work for every chunk can be
        dispatched to a pool before anything is written (see
        :meth:`_summarize_chunks_concurrently`).
        """
        joined = "\n\n".join(_message_text(m) for m in messages)
        chunk_id = self._chunk_id_for(start, end, messages)
        chunk_path = self._chunks_dir() / f"{chunk_id}.json"
        cached = self._read_json(chunk_path)
        if isinstance(cached, dict) and cached.get("summary"):
            # Identical range + identical bytes ⇒ identical chunk_id, and the
            # chunk (with its summary) is already on disk. Compression re-archives
            # the whole transcript on every pass, so summarizing again burned one
            # LLM call per chunk per pass on inputs that had not changed —
            # measured: a byte-identical second pass still fired the summarizer.
            return {
                "messages": messages,
                "start": start,
                "end": end,
                "source": source,
                "joined": joined,
                "chunk_id": chunk_id,
                "chunk_path": chunk_path,
                "allow_llm": False,
                "cached_entry": self._chunk_entry(
                    chunk_id,
                    start,
                    end,
                    str(cached["summary"]),
                    cached.get("messages") or messages,
                    cached.get("created_at"),
                    str(cached.get("summary_kind") or "llm"),
                ),
            }
        return {
            "messages": messages,
            "start": start,
            "end": end,
            "source": source,
            "joined": joined,
            "chunk_id": chunk_id,
            "chunk_path": chunk_path,
            "allow_llm": bool(allow_llm),
            "cached_entry": None,
        }

    def _summarize_chunks_concurrently(
        self, prepared: List[Dict[str, Any]]
    ) -> List[Optional[str]]:
        """Summarise every LLM-eligible chunk, at most ``llm_summary_concurrency`` at a time.

        Returns one entry per prepared chunk — ``None`` where no LLM call was
        allowed or the call failed, in which case the caller writes the
        deterministic recap. Results are collected in submission order, so a
        summary can never land on the wrong chunk.
        """
        results: List[Optional[str]] = [None] * len(prepared)
        pending = [i for i, item in enumerate(prepared) if item.get("allow_llm")]
        if not pending:
            return results
        workers = max(1, min(int(self.llm_summary_concurrency), len(pending)))
        if workers == 1:
            for index in pending:
                item = prepared[index]
                results[index] = self._summarize_chunk(
                    item["joined"], messages=item["messages"]
                )
            return results
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="archive-summary"
        ) as pool:
            futures = {
                index: pool.submit(
                    self._summarize_chunk,
                    prepared[index]["joined"],
                    messages=prepared[index]["messages"],
                )
                for index in pending
            }
            for index, future in futures.items():
                try:
                    results[index] = future.result()
                except Exception:
                    logger.debug(
                        "chunk summary %s failed; falling back to a recap",
                        prepared[index]["chunk_id"],
                        exc_info=True,
                    )
        return results

    def _write_prepared_chunk(
        self, item: Dict[str, Any], summary: Optional[str]
    ) -> Dict[str, Any]:
        """Persist one chunk unconditionally and return its index entry."""
        if item.get("cached_entry") is not None:
            return item["cached_entry"]
        if summary is None:
            # Past the pass budget (or summaries disabled/failed): keep the chunk
            # and its deterministic recap, skip the model call.
            summary = self._fallback_summary(item["messages"], item["joined"])
        summary_kind = "recap" if summary.startswith(_RECAP_PREFIX) else "llm"
        created_at = time.time()
        payload = {
            "chunk_id": item["chunk_id"],
            "session_id": self._session_id,
            "source": item["source"],
            "created_at": created_at,
            "message_start": item["start"],
            "message_end": item["end"],
            "summary": summary,
            "summary_kind": summary_kind,
            "messages": item["messages"],
        }
        atomic_json_write(item["chunk_path"], payload, indent=2, mode=0o600)
        return self._chunk_entry(
            item["chunk_id"],
            item["start"],
            item["end"],
            summary,
            item["messages"],
            created_at,
            summary_kind,
        )

    def _store_chunk(
        self,
        messages: List[Dict[str, Any]],
        start: int,
        end: int,
        *,
        source: str,
        allow_llm: bool = True,
    ) -> Dict[str, Any]:
        """Single-chunk path: prepare → summarise → write."""
        item = self._prepare_chunk(
            messages, start, end, source=source, allow_llm=allow_llm
        )
        summary = (
            self._summarize_chunk(item["joined"], messages=item["messages"])
            if item.get("allow_llm")
            else None
        )
        return self._write_prepared_chunk(item, summary)

    def _chunk_entry(
        self,
        chunk_id: str,
        start: int,
        end: int,
        summary: str,
        messages: List[Dict[str, Any]],
        created_at: Any,
        summary_kind: str = "llm",
    ) -> Dict[str, Any]:
        """Build the index entry for a chunk (no LLM work)."""
        joined = "\n\n".join(
            _message_text(m) for m in messages if isinstance(m, dict)
        )
        preview = redact_sensitive_text(joined[:700].replace("\n", " "), force=True)
        return {
            "chunk_id": chunk_id,
            "created_at": created_at or time.time(),
            "message_start": start,
            "message_end": end,
            "summary": summary,
            "summary_kind": summary_kind,
            "preview": preview,
            "search_text": f"{summary}\n{joined[:20_000]}",
        }

    def _summarize_chunk(self, text: str, *, messages: List[Dict[str, Any]]) -> str:
        cfg = self._summary_config()
        if not cfg.get("enabled"):
            return self._fallback_summary(messages, text)
        if self._llm_summary_budget_exhausted():
            return self._fallback_summary(messages, text)
        sample = text[:_bounded_int(cfg.get("max_input_chars"), 12_000, 1000, 80_000)]
        prompt = (
            "Summarize this Hermes transcript chunk for later long-horizon "
            "context reconstruction. Preserve task phase, concrete decisions, "
            "claims, evidence references, file paths, commands, failures, and "
            "open questions. Do not invent facts. Return concise plain text "
            "with sections: Phase, Claims/Evidence, Decisions, Open Questions.\n\n"
            f"{sample}"
        )
        try:
            from agent.auxiliary_client import call_llm, extract_content_or_reasoning

            with self._llm_summary_lock:
                self._llm_summary_calls += 1
            response = call_llm(
                task="session_archive_summary",
                provider=cfg.get("provider") or None,
                model=cfg.get("model") or None,
                base_url=cfg.get("base_url") or None,
                api_key=cfg.get("api_key") or None,
                api_mode=cfg.get("api_mode") or None,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You write compact, evidence-preserving recap "
                            "notes for an AI agent's archived conversation."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=_bounded_int(cfg.get("max_tokens"), 500, 100, 2000),
                timeout=float(cfg.get("timeout") or 30),
            )
            summary = (extract_content_or_reasoning(response) or "").strip()
        except Exception as exc:
            logger.debug("session_archive chunk summary failed: %s", exc)
            summary = ""
        if not summary:
            return self._fallback_summary(messages, text)
        return redact_sensitive_text(summary[:4000], force=True, redact_url_credentials=True)

    def _fallback_summary(self, messages: List[Dict[str, Any]], text: str) -> str:
        roles: Dict[str, int] = {}
        for message in messages:
            role = str(message.get("role") or "unknown")
            roles[role] = roles.get(role, 0) + 1
        role_text = ", ".join(f"{role}:{count}" for role, count in sorted(roles.items()))
        preview = redact_sensitive_text(
            text[:500].replace("\n", " "),
            force=True,
            redact_url_credentials=True,
        )
        return f"{_RECAP_PREFIX}{role_text}): {preview}"

    def _maybe_write_secondary_index(self) -> None:
        index = self._read_index()
        if len(index) < self.secondary_index_chunk_threshold:
            return
        # Reuse a group's recap when its inputs are unchanged. The groups are
        # recomputed from the whole index on EVERY compression pass, so without
        # this the stage-level recaps cost one LLM call per group per pass for
        # as long as the session lives.
        previous = {
            str(item.get("group_id")): item
            for item in self._read_secondary_index()
            if isinstance(item, dict) and item.get("group_id")
        }
        groups = []
        for group_index, start in enumerate(
            range(0, len(index), self.secondary_index_group_size),
            start=1,
        ):
            items = index[start:start + self.secondary_index_group_size]
            if not items:
                continue
            summaries = "\n".join(
                f"- {item.get('chunk_id')}: {item.get('summary') or item.get('preview') or ''}"
                for item in items
            )
            group_id = f"recap-{group_index}"
            fingerprint = hashlib.sha256(
                summaries.encode("utf-8", "replace")
            ).hexdigest()[:16]
            prior = previous.get(group_id) or {}
            if prior.get("fingerprint") == fingerprint and prior.get("summary"):
                summary = str(prior["summary"])
            else:
                summary = self._summarize_secondary_group(summaries)
            groups.append(
                {
                    "group_id": group_id,
                    "chunk_ids": [item.get("chunk_id") for item in items],
                    "message_start": items[0].get("message_start"),
                    "message_end": items[-1].get("message_end"),
                    "summary": summary,
                    "fingerprint": fingerprint,
                    "updated_at": time.time(),
                }
            )
        atomic_json_write(self._secondary_index_path(), groups, indent=2, mode=0o600)

    def _summarize_secondary_group(self, summaries: str) -> str:
        cfg = self._summary_config()
        if not cfg.get("enabled"):
            return summaries[:3000]
        # Stage recaps share the pass budget with chunk summaries — they are the
        # same critical-path LLM calls, just at a coarser grain.
        if self._llm_summary_budget_exhausted():
            return summaries[:3000]
        try:
            from agent.auxiliary_client import call_llm, extract_content_or_reasoning

            with self._llm_summary_lock:
                self._llm_summary_calls += 1
            response = call_llm(
                task="session_archive_summary",
                provider=cfg.get("provider") or None,
                model=cfg.get("model") or None,
                base_url=cfg.get("base_url") or None,
                api_key=cfg.get("api_key") or None,
                api_mode=cfg.get("api_mode") or None,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You compress several archived chunk recaps into "
                            "one higher-level stage recap for progressive disclosure."
                        ),
                    },
                    {"role": "user", "content": summaries[:12_000]},
                ],
                temperature=0,
                max_tokens=_bounded_int(cfg.get("secondary_max_tokens"), 700, 100, 2500),
                timeout=float(cfg.get("timeout") or 30),
            )
            text = (extract_content_or_reasoning(response) or "").strip()
            if text:
                return redact_sensitive_text(text[:5000], force=True, redact_url_credentials=True)
        except Exception as exc:
            logger.debug("session_archive secondary summary failed: %s", exc)
        return summaries[:3000]

    def _merge_index(self, chunks: List[Dict[str, Any]]) -> None:
        existing = self._read_index()
        by_id = {item.get("chunk_id"): item for item in existing if item.get("chunk_id")}
        for chunk in chunks:
            by_id[chunk["chunk_id"]] = chunk
        merged = sorted(by_id.values(), key=lambda item: (item.get("created_at") or 0, item.get("chunk_id") or ""))
        atomic_json_write(self._index_path(), merged, indent=2, mode=0o600)

    def _read_json(self, path: Path) -> Any:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _read_index(self) -> List[Dict[str, Any]]:
        path = self._index_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def _read_reports(self) -> List[Dict[str, Any]]:
        path = self._reports_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def _read_secondary_index(self) -> List[Dict[str, Any]]:
        path = self._secondary_index_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def _summary_config(self) -> Dict[str, Any]:
        cfg = self._config.get("llm_summary", {})
        if not isinstance(cfg, dict):
            cfg = {}
        merged = dict(cfg)
        merged["enabled"] = bool(merged.get("enabled", False))
        return merged

    def _load_config(self) -> Dict[str, Any]:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
            memory_cfg = config.get("memory", {}) if isinstance(config, dict) else {}
            archive_cfg = (
                memory_cfg.get("session_archive", {})
                if isinstance(memory_cfg, dict)
                else {}
            )
            return dict(archive_cfg) if isinstance(archive_cfg, dict) else {}
        except Exception:
            return {}

    def _session_dir(self) -> Path:
        root = self._root or get_hermes_home() / "session_archive"
        return root / self._session_id

    def _chunks_dir(self) -> Path:
        path = self._session_dir() / "chunks"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _index_path(self) -> Path:
        return self._session_dir() / "index.json"

    def _reports_path(self) -> Path:
        return self._session_dir() / "delegation_reports.json"

    def _secondary_index_path(self) -> Path:
        return self._session_dir() / "secondary_index.json"


def register(ctx) -> None:
    ctx.register_memory_provider(SessionArchiveProvider())
