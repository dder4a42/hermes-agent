"""LLM-driven task-add extractor.

Takes free-form user text (optionally paired with a partial draft from an
earlier turn) and asks a single LLM call to normalise it into structured
reminder fields AND decide whether more information is needed before
committing the task to storage.

Design contract
---------------
Input:
  raw_text        the message the user just sent
  draft           the partial task dict accumulated from previous turns
                  (empty for a fresh /s add)
  now, tz         reference clock + timezone for schedule normalisation
  model, provider LLM to call (defaults to deepseek-chat)

Output:
  ExtractResult
      fields          merged draft (existing fields + newly extracted)
      next_prompt     next question to ask the user (Chinese, one line)
                      OR None when the LLM judges the draft complete
      done            True when the LLM says commit; False otherwise
      confidence      0.0..1.0 self-reported
      reasoning       short debug string
      error           set when the LLM call itself failed

The extractor NEVER raises for content reasons. Callers treat error != None
as 'model unreachable; keep the raw text, fall back to Usage message'.

Config (env, mirrors schedule_parser layout):
  TASK_EXTRACTOR_MODEL       default 'deepseek-chat'
  TASK_EXTRACTOR_PROVIDER    informational (default 'deepseek')
  TASK_EXTRACTOR_BASE_URL    default 'https://api.deepseek.com/v1'
  DEEPSEEK_API_KEY           required unless a test hook is installed
  TASK_EXTRACTOR_TIMEOUT     seconds, default 25
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "deepseek-v4-flash"
_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEFAULT_TIMEOUT_S = 25.0

_ALLOWED_FIELDS = {
    "title", "schedule_raw", "notes", "url", "location",
    "attendees", "tags", "remind_before_min", "checklist",
    "recurrence",
}


@dataclass
class ExtractResult:
    ok: bool
    fields: dict = field(default_factory=dict)
    next_prompt: Optional[str] = None
    done: bool = False
    confidence: float = 0.0
    reasoning: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "fields": self.fields,
            "next_prompt": self.next_prompt,
            "done": self.done,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "error": self.error,
        }


_llm_call_impl: Optional[Callable[..., str]] = None


def set_llm_call_for_tests(fn: Optional[Callable[..., str]]) -> None:
    """Test hook — replace the network call with a canned responder."""
    global _llm_call_impl
    _llm_call_impl = fn


def _default_llm_call(system: str, user: str, model: str, base_url: str,
                     api_key: str, timeout_s: float) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=800,
        temperature=0.2,
    )
    return resp.choices[0].message.content or ""


_SYSTEM_PROMPT = """You are a task-add slot-filler for a personal reminder bot.
Your ONLY job is to look at the user's free-form message (plus any partial
draft the bot has already collected) and decide:
  (a) what structured fields to update in the draft;
  (b) whether the draft is complete enough to commit — and if not, what
      ONE question to ask the user next.

You will be given:
  now_iso       current time as ISO with tz offset.
  tz_name       user's timezone (e.g. Asia/Shanghai).
  turn          the 1-based turn number in the ongoing draft session.
  max_turns     when turn == max_turns, YOU MUST return done=true and commit
                whatever you have — no more questions.
  draft         current partial task dict, may be empty {}.
  user_text     the message you must interpret.

Output a single compact JSON object with EXACTLY these keys:
  fields         object with any subset of:
                   title (string), schedule_raw (string, the user's phrase
                   for when — leave the parsing to a downstream module),
                   notes (string), url (string), location (string),
                   attendees (list[string]), tags (list[string]),
                   remind_before_min (integer minutes or expression string
                   like '30m'/'1h'/'2小时'), checklist (list[string]),
                   recurrence (one of once|daily|weekly|monthly|yearly).
                 OMIT keys the user did not provide; do NOT invent values.
                 Merge the user's new info with the existing draft.
                 For lists (attendees/tags/checklist), return the FULL
                 updated list (draft + new), not just deltas.
  next_prompt    the ONE follow-up question to ask, in Chinese, one short
                 line — or null if done is true.
  done           true when you have enough to save the reminder. Minimum
                 for done: a non-empty title AND a non-empty schedule_raw.
                 Prefer done=true when the user's text is a full sentence
                 (e.g. "明天下午3点跟 Bob 开会") — do not ask redundant
                 questions when the info is already there.
  confidence     0.0..1.0.
  reasoning      one short sentence.

Slot-filling heuristics:
  * A user message like "明天下午3点跟 Bob 开会" gives you title, schedule_raw,
    and one attendee in one shot. That's done=true unless it's a meeting
    where join-info is clearly missing AND the user has hinted at needing
    it (e.g. contains "会议" / "meeting" / "call").
  * For meeting-flavored tasks, if url is missing, you MAY ask "入会方式？
    (腾讯会议链接/线下地点/skip)". Do NOT ask more than one field per turn.
  * For deadline-flavored tasks ("截止", "deadline", "交", "before"), you
    MAY ask "需要提前多久提醒你？(默认 skip)".
  * Never ask about optional fields the user has not signalled interest in.
  * If the user replies "skip" / "不用" / "no" to your last question, treat
    that field as skipped and move on (usually done=true).
  * If turn >= max_turns, force done=true regardless of missing fields —
    commit whatever you have.
  * The user's own message may include "cancel" / "取消" — in that case set
    fields.cancel=true, done=false, next_prompt="已取消。".

Respond with ONLY the JSON. No markdown, no prose, no code fences."""


def _now_iso(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.isoformat(timespec="seconds")


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)


def _strip_code_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return raw.strip()


def _extract_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    text = _strip_code_fences(raw)
    try:
        return json.loads(text)
    except Exception:
        m = _JSON_OBJECT_RE.search(text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def _merge_fields(existing: dict, incoming: dict) -> dict:
    """Merge extractor output into the running draft.

    * String fields overwrite when incoming has a non-empty value.
    * List fields overwrite entirely (extractor returns full updated list).
    * Cancel flag propagates.
    """
    out = dict(existing or {})
    for k, v in (incoming or {}).items():
        if k not in _ALLOWED_FIELDS and k != "cancel":
            continue
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        out[k] = v
    return out


def extract_fields(
    raw_text: str,
    *,
    draft: Optional[dict] = None,
    now: Optional[datetime] = None,
    tz_name: str = "Asia/Shanghai",
    turn: int = 1,
    max_turns: int = 5,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout_s: Optional[float] = None,
) -> ExtractResult:
    """Ask the LLM to slot-fill a task draft from `raw_text`.

    Empty raw_text short-circuits without a network call.
    """
    draft = dict(draft or {})
    if not raw_text or not raw_text.strip():
        return ExtractResult(ok=True, fields=draft, next_prompt=None, done=False,
                             confidence=0.0, reasoning="empty user text")

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone(timedelta(hours=8)) if "Shang" in tz_name else timezone.utc

    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    model = (model or os.environ.get("TASK_EXTRACTOR_MODEL") or _DEFAULT_MODEL).strip()
    base_url = (base_url or os.environ.get("TASK_EXTRACTOR_BASE_URL") or _DEFAULT_BASE_URL).strip()
    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
    timeout_s = timeout_s if timeout_s is not None else float(
        os.environ.get("TASK_EXTRACTOR_TIMEOUT") or _DEFAULT_TIMEOUT_S
    )

    if not api_key and _llm_call_impl is None:
        return ExtractResult(
            ok=False, fields=draft, error="missing DEEPSEEK_API_KEY / TASK_EXTRACTOR_* config",
        )

    user_prompt = (
        f"now_iso: {_now_iso(now)}\n"
        f"tz_name: {tz_name}\n"
        f"turn: {int(turn)}\n"
        f"max_turns: {int(max_turns)}\n"
        f"draft: {json.dumps(draft, ensure_ascii=False)}\n"
        f"user_text: {raw_text!r}\n"
    )

    call = _llm_call_impl or _default_llm_call
    try:
        raw = call(_SYSTEM_PROMPT, user_prompt, model, base_url, api_key, timeout_s)
    except Exception as exc:
        logger.warning("task_extractor: LLM call failed model=%s: %s", model, exc)
        return ExtractResult(
            ok=False, fields=draft,
            error=f"LLM call failed: {type(exc).__name__}: {exc}",
        )

    payload = _extract_json(raw)
    if payload is None:
        logger.warning("task_extractor: no JSON in response: %r", raw[:200])
        return ExtractResult(ok=False, fields=draft, error="LLM returned no JSON")

    incoming_fields = payload.get("fields") or {}
    if not isinstance(incoming_fields, dict):
        incoming_fields = {}

    merged = _merge_fields(draft, incoming_fields)

    cancelled = bool(incoming_fields.get("cancel"))
    done = bool(payload.get("done", False))
    next_prompt = payload.get("next_prompt")
    if isinstance(next_prompt, str):
        next_prompt = next_prompt.strip() or None
    else:
        next_prompt = None

    # Force-close if we've hit the turn cap.
    if turn >= max_turns and not cancelled:
        done = True
        next_prompt = None

    # Defensive: if done=true and we're missing the two hard-required slots
    # (title / schedule_raw), demote done to false and ask a targeted follow-up
    # rather than trust the model.
    if done and not cancelled:
        missing = []
        if not (merged.get("title") or "").strip():
            missing.append("title")
        if not (merged.get("schedule_raw") or "").strip():
            missing.append("when")
        if missing:
            done = False
            if next_prompt is None:
                if "title" in missing:
                    next_prompt = "这个提醒的内容是什么？"
                else:
                    next_prompt = "什么时间？"

    try:
        confidence = float(payload.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reasoning = str(payload.get("reasoning") or "").strip()

    return ExtractResult(
        ok=True,
        fields=merged,
        next_prompt=None if cancelled else next_prompt,
        done=done and not cancelled,
        confidence=confidence,
        reasoning=reasoning,
    )
