"""LLM-driven schedule expression parser.

Users write reminders in free-form time expressions — 'tomorrow 3pm',
'明天下午3点', 'in 30 minutes', '每周三下午2点'. This module normalises any of
those into an absolute ISO timestamp (and optional cron expression for
recurring cases) via a small LLM call, so a downstream surfacer can decide
whether a task is due purely by comparing datetimes.

Design contract:
  * Input: raw string + reference 'now' + timezone.
  * Output: ScheduleResult with scheduled_at, schedule_cron, recurrence,
    confidence, reasoning, method. Or ScheduleResult.failure(reason) if the
    LLM call fails, output is malformed, or the parsed time is unreasonable.
  * The parser NEVER raises for content reasons — callers should treat a
    failed result as 'user must clarify' and keep the raw expression around.

Config (environment):
  RESEARCH_COPILOT_SCHEDULE_MODEL      default 'deepseek-chat'
  RESEARCH_COPILOT_SCHEDULE_PROVIDER   default 'deepseek'  (informational)
  RESEARCH_COPILOT_SCHEDULE_BASE_URL   default 'https://api.deepseek.com/v1'
  DEEPSEEK_API_KEY                     required for the default provider
  RESEARCH_COPILOT_SCHEDULE_TIMEOUT    seconds, default 20
  RESEARCH_COPILOT_SCHEDULE_MAX_YEARS  max future years, default 1
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

_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEFAULT_TIMEOUT_S = 20.0
_DEFAULT_MAX_FUTURE_YEARS = 1
# Allow slightly-past reference for edits like 'that just passed'; tighter
# than the fuzzy 'weekend' case which yields a future datetime by design.
_PAST_TOLERANCE_MIN = 5


_ALLOWED_RECURRENCES = frozenset({"once", "daily", "weekly", "monthly", "yearly"})


@dataclass
class ScheduleResult:
    ok: bool
    scheduled_at: Optional[str] = None      # ISO datetime with offset
    schedule_cron: Optional[str] = None     # 5-field, only when recurrence != once
    recurrence: str = "once"
    confidence: float = 0.0
    reasoning: str = ""
    method: str = ""                        # 'llm' | 'llm-failed' | ...
    error: str = ""

    @classmethod
    def failure(cls, error: str, method: str = "llm-failed") -> "ScheduleResult":
        return cls(ok=False, error=error, method=method)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "scheduled_at": self.scheduled_at,
            "schedule_cron": self.schedule_cron,
            "recurrence": self.recurrence,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "method": self.method,
            "error": self.error,
        }


# Injection hook for tests. Signature: (system, user_prompt, model, base_url,
# api_key, timeout_s) -> str raw content. Real implementation lives in
# _default_llm_call below.
_llm_call_impl: Optional[Callable[..., str]] = None


def _default_llm_call(system: str, user: str, model: str, base_url: str,
                     api_key: str, timeout_s: float) -> str:
    from openai import OpenAI  # lazy — 240ms cold import
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_tokens=500,
        temperature=0.1,
    )
    return resp.choices[0].message.content or ""


def set_llm_call_for_tests(fn: Optional[Callable[..., str]]) -> None:
    """Tests plug in a canned responder so no network is touched."""
    global _llm_call_impl
    _llm_call_impl = fn


_SYSTEM_PROMPT = """You are a time-expression parser. Convert a user's schedule
phrase into a strict JSON object. Input can be English or Chinese.

You will be given:
  now_iso  — the reference 'current' time as ISO with timezone offset.
  tz_name  — the user's timezone name (e.g. Asia/Shanghai).

Output a single compact JSON object with EXACTLY these keys:
  scheduled_at   string, ISO 8601 with timezone offset (e.g. '2026-07-14T15:00:00+08:00').
                 MUST be at or after now_iso (allow up to 5 minutes in the past to
                 accommodate 'just now' phrasing). MUST NOT be more than 1 year
                 after now_iso. Preserve the user's timezone (tz_name offset).
  schedule_cron  5-field cron expression when recurrence != 'once', else null.
                 Use the same timezone as tz_name (minute hour dom month dow).
  recurrence     one of: 'once', 'daily', 'weekly', 'monthly', 'yearly'.
  confidence     0.0..1.0 float. High for exact times, low for very fuzzy
                 phrases ('sometime next week').
  reasoning      1-2 short sentences explaining what you assumed.

Ambiguity rules:
  - Bare '下午两点' / '2pm' with no date: today if that time is still in the
    future, otherwise tomorrow.
  - '周末' / 'this weekend': Saturday 10:00 local of the coming weekend.
  - '晚上' defaults to 20:00. '下午' defaults to 14:00. '上午' defaults to 09:00.
  - '晚上晚一点儿' → 21:00; '晚上十一点' → 23:00.
  - 'in N minutes/hours/days' → now + delta.
  - '每周三下午2点' → recurrence='weekly', schedule_cron='0 14 * * 3',
    scheduled_at = next Wednesday 14:00.

Respond with ONLY the JSON. No markdown, no prose, no code fences.
"""


def _now_iso_with_tz(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.isoformat(timespec="seconds")


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)


def _strip_code_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        # Remove leading and trailing fence lines.
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
        match = _JSON_OBJECT_RE.search(text)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except Exception:
            return None


def _parse_iso(dt_str: str) -> Optional[datetime]:
    if not isinstance(dt_str, str):
        return None
    # Python <=3.10 doesn't accept 'Z'; normalise.
    s = dt_str.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def parse_schedule(
    raw: str,
    *,
    now: Optional[datetime] = None,
    tz_name: str = "Asia/Shanghai",
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout_s: Optional[float] = None,
    max_future_years: Optional[int] = None,
) -> ScheduleResult:
    """Ask the LLM to normalise ``raw`` into an absolute schedule."""
    if not raw or not raw.strip():
        return ScheduleResult.failure("empty schedule expression")

    # tz handling — try zoneinfo, fall back to fixed offset for CST.
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone(timedelta(hours=8)) if "Shang" in tz_name else timezone.utc

    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    model = (model or os.environ.get("RESEARCH_COPILOT_SCHEDULE_MODEL") or _DEFAULT_MODEL).strip()
    base_url = (base_url or os.environ.get("RESEARCH_COPILOT_SCHEDULE_BASE_URL") or _DEFAULT_BASE_URL).strip()
    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or ""
    timeout_s = timeout_s if timeout_s is not None else float(
        os.environ.get("RESEARCH_COPILOT_SCHEDULE_TIMEOUT") or _DEFAULT_TIMEOUT_S
    )
    max_future_years = int(max_future_years if max_future_years is not None else
                           os.environ.get("RESEARCH_COPILOT_SCHEDULE_MAX_YEARS") or
                           _DEFAULT_MAX_FUTURE_YEARS)

    if not api_key and _llm_call_impl is None:
        return ScheduleResult.failure(
            "missing DEEPSEEK_API_KEY (or RESEARCH_COPILOT_SCHEDULE_* provider config)"
        )

    user_prompt = (
        f"now_iso: {_now_iso_with_tz(now)}\n"
        f"tz_name: {tz_name}\n"
        f"raw: {raw!r}\n"
    )

    call = _llm_call_impl or _default_llm_call
    try:
        raw_content = call(_SYSTEM_PROMPT, user_prompt, model, base_url, api_key, timeout_s)
    except Exception as exc:
        logger.warning("schedule_parser: LLM call failed model=%s: %s", model, exc)
        return ScheduleResult.failure(f"LLM call failed: {type(exc).__name__}: {exc}")

    payload = _extract_json(raw_content)
    if payload is None:
        logger.warning("schedule_parser: no JSON in response: %r", raw_content[:200])
        return ScheduleResult.failure("LLM returned no JSON")

    scheduled_at_str = payload.get("scheduled_at")
    scheduled_dt = _parse_iso(scheduled_at_str)
    if scheduled_dt is None:
        return ScheduleResult.failure(f"invalid scheduled_at: {scheduled_at_str!r}")
    if scheduled_dt.tzinfo is None:
        scheduled_dt = scheduled_dt.replace(tzinfo=tz)

    # Sanity: within [now - 5min, now + max_future_years].
    if scheduled_dt < now - timedelta(minutes=_PAST_TOLERANCE_MIN):
        return ScheduleResult.failure(
            f"scheduled_at is in the past: {scheduled_dt.isoformat()}"
        )
    if scheduled_dt > now + timedelta(days=365 * max_future_years + 1):
        return ScheduleResult.failure(
            f"scheduled_at exceeds {max_future_years}-year horizon: {scheduled_dt.isoformat()}"
        )

    recurrence = str(payload.get("recurrence") or "once").strip().lower() or "once"
    if recurrence not in _ALLOWED_RECURRENCES:
        logger.warning("schedule_parser: coercing unknown recurrence %r to 'once'", recurrence)
        recurrence = "once"

    cron_expr = payload.get("schedule_cron")
    if recurrence == "once":
        cron_expr = None
    elif cron_expr:
        cron_expr = str(cron_expr).strip() or None

    try:
        confidence = float(payload.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reasoning = str(payload.get("reasoning") or "").strip()

    return ScheduleResult(
        ok=True,
        scheduled_at=scheduled_dt.isoformat(timespec="seconds"),
        schedule_cron=cron_expr,
        recurrence=recurrence,
        confidence=confidence,
        reasoning=reasoning,
        method="llm",
    )
