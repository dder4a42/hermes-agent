"""Interactive /s add draft session.

Holds a per-user in-memory draft that survives across turns so the LLM
extractor can slot-fill a reminder over several messages instead of
requiring one perfectly-formed CLI invocation.

State lives in a module-level dict — the gateway runs as a single
process, so there's no cross-process coherence to worry about. TTL is
30 minutes; drafts older than that are ignored / cleared lazily on any
access. There's also a hard cap on turns (default 5) that the extractor
enforces so a runaway draft can't loop forever.

Public API:
    has_active(user_id) -> bool
    start_or_advance(user_id, user_text, seed=None) -> DraftReply
    cancel(user_id) -> bool
    peek(user_id) -> TaskDraft | None
    sweep() -> int      # returns number of expired drafts removed
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_TTL = timedelta(minutes=30)
_MAX_TURNS = 5


@dataclass
class TaskDraft:
    user_id: str
    fields: dict = field(default_factory=dict)
    turn: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_touched: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Verbatim user messages per turn — persisted alongside the task so we
    # can audit what the LLM rewrote it into. Joined with " / ".
    input_history: list = field(default_factory=list)


@dataclass
class DraftReply:
    text: str
    committed: bool = False
    cancelled: bool = False
    draft: Optional[TaskDraft] = None


_drafts: dict[str, TaskDraft] = {}
_lock = threading.RLock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(d: TaskDraft) -> bool:
    return (_now() - d.last_touched) > _TTL


def peek(user_id: str) -> Optional[TaskDraft]:
    with _lock:
        d = _drafts.get(user_id)
        if d is None:
            return None
        if _is_expired(d):
            _drafts.pop(user_id, None)
            return None
        return d


def has_active(user_id: str) -> bool:
    return peek(user_id) is not None


def cancel(user_id: str) -> bool:
    with _lock:
        return _drafts.pop(user_id, None) is not None


def sweep() -> int:
    """Drop any expired drafts. Callers can call this periodically."""
    n = 0
    with _lock:
        for uid, d in list(_drafts.items()):
            if _is_expired(d):
                _drafts.pop(uid, None)
                n += 1
    return n


def _summary_line(fields: dict) -> str:
    parts = []
    title = fields.get("title")
    if title:
        parts.append(str(title))
    when = fields.get("schedule_raw")
    if when:
        parts.append(f"⏰ {when}")
    attendees = fields.get("attendees") or []
    if attendees:
        parts.append("👥 " + ", ".join(attendees))
    url = fields.get("url")
    if url:
        parts.append(f"🔗 {url}")
    return " · ".join(parts) or "(空 draft)"


def _commit(user_id: str, draft: TaskDraft) -> DraftReply:
    """Persist the draft into thoughts.json via tools.thought_tools.add_task."""
    from tools.thought_tools import add_task

    fields = draft.fields or {}
    title = str(fields.get("title") or "").strip()
    schedule_raw = str(fields.get("schedule_raw") or "").strip()
    if not title or not schedule_raw:
        # Should not happen — extractor demotes done=true in this case — but
        # be defensive so we never save an empty task.
        return DraftReply(
            text="⚠ 内部错误：缺少 title 或 when，草稿未保存。用 /s cancel 放弃或再补充信息。",
            draft=draft,
        )

    task = add_task(
        title=title,
        schedule_raw=schedule_raw,
        notes=str(fields.get("notes") or ""),
        recurrence=str(fields.get("recurrence") or "once"),
        url=str(fields.get("url") or ""),
        location=str(fields.get("location") or ""),
        attendees=fields.get("attendees") or [],
        tags=fields.get("tags") or [],
        remind_before_min=fields.get("remind_before_min") or 0,
        checklist=fields.get("checklist") or [],
        input_raw=" / ".join(draft.input_history or []),
    )
    _drafts.pop(user_id, None)

    perr = (task.get("parse") or {}).get("error")
    lines = [f"✅ 已添加 **{title}** (`{task['id']}`)"]
    if task.get("scheduled_at"):
        lines.append(f"⏰ {task['scheduled_at']}")
    elif perr:
        lines.append(f"⚠ 时间解析失败: {perr}")
    if task.get("url"):
        lines.append(f"🔗 {task['url']}")
    if task.get("location"):
        lines.append(f"📍 {task['location']}")
    attendees = task.get("attendees") or []
    if attendees:
        lines.append(f"👥 {', '.join(attendees)}")
    if task.get("remind_before_min"):
        lines.append(f"🔔 会前 {task['remind_before_min']} 分钟提醒")
    checklist = task.get("checklist") or []
    if checklist:
        lines.append("📋 checklist:")
        for it in checklist:
            box = "[x]" if it.get("done") else "[ ]"
            lines.append(f"  {box} {it.get('text', '')}")
    return DraftReply(text="\n".join(lines), committed=True)


def start_or_advance(
    user_id: str,
    user_text: str,
    *,
    seed: Optional[dict] = None,
    max_turns: int = _MAX_TURNS,
) -> DraftReply:
    """Advance an existing draft or open a new one.

    ``seed`` supplies initial field values (e.g. from CLI flag parsing on
    the very first /s add call). Ignored when a draft already exists.
    ``user_text`` is fed to the LLM extractor.
    """
    from tools.task_extractor import extract_fields

    with _lock:
        draft = _drafts.get(user_id)
        if draft is not None and _is_expired(draft):
            _drafts.pop(user_id, None)
            draft = None
        if draft is None:
            draft = TaskDraft(user_id=user_id, fields=dict(seed or {}))
            _drafts[user_id] = draft
        draft.turn += 1
        draft.last_touched = _now()
        if user_text and user_text.strip():
            draft.input_history.append(user_text.strip())
        current_turn = draft.turn
        current_fields = dict(draft.fields)

    result = extract_fields(
        user_text,
        draft=current_fields,
        turn=current_turn,
        max_turns=max_turns,
    )

    if not result.ok:
        # LLM unreachable — leave the draft in place so the user can retry.
        return DraftReply(
            text=f"⚠ 无法解析你的输入（{result.error}）。稍后再试或用 /s cancel 放弃。",
            draft=draft,
        )

    if result.fields.get("cancel"):
        _drafts.pop(user_id, None)
        return DraftReply(text="已取消当前草稿。", cancelled=True)

    with _lock:
        d = _drafts.get(user_id)
        if d is not None:
            d.fields = {k: v for k, v in result.fields.items() if k != "cancel"}
            d.last_touched = _now()
            draft = d

    if result.done:
        return _commit(user_id, draft)

    prompt = result.next_prompt or "还需要什么？"
    summary = _summary_line(draft.fields)
    return DraftReply(
        text=f"{prompt}\n\n📝 目前收集到: {summary}\n(说 /s cancel 放弃)",
        draft=draft,
    )
