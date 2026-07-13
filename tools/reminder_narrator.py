"""One-shot LLM narrator for reminder delivery.

The static surfacer template (⏰ Reminder: <title> + emoji field list) is
correct and information-dense but reads like a machine log. This module
adds a single natural-language opener written by the LLM, so the delivered
message feels like a note from an assistant instead of a JSON dump. The
structured fields (checklist / URL / attendees) still get appended after
the narrative line — the narrator does not replace them.

Design contract:
    narrate_reminder(task, mode='main', minutes_left=0) -> str | None
        Returns a single-line string, in the user's language, ≤80 chars,
        no emoji, no markdown. Falls back to None when the LLM call fails
        or the response is empty; callers should then fall back to the
        static template.

Configuration (env, mirrors schedule_parser / task_extractor):
    REMINDER_NARRATOR_MODEL        default 'deepseek-v4-flash'
    REMINDER_NARRATOR_PROVIDER     informational, default 'deepseek'
    REMINDER_NARRATOR_BASE_URL     default 'https://api.deepseek.com/v1'
    DEEPSEEK_API_KEY               required
    REMINDER_NARRATOR_TIMEOUT      seconds, default 12
    REMINDER_NARRATOR_MAX_CHARS    hard trim, default 80
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "deepseek-v4-flash"
_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEFAULT_TIMEOUT_S = 12.0
_DEFAULT_MAX_CHARS = 80


_llm_call_impl: Optional[Callable[..., str]] = None


def set_llm_call_for_tests(fn: Optional[Callable[..., str]]) -> None:
    """Replace the network call with a canned responder in tests."""
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
        max_tokens=800,
        temperature=0.5,
    )
    return resp.choices[0].message.content or ""


_SYSTEM_MAIN = """You rewrite a scheduled reminder into ONE natural sentence
that a friendly personal assistant would send to the user right when the
task starts. Match the user's language (Chinese if the title is Chinese).

You MUST return exactly one sentence — never an empty string, never a
question. Even when the input is a bare title with no extra context, you
must invent nothing beyond the title but you must still write ONE
natural sentence about starting that action.

Rules:
  * ONE line. No line breaks. No emoji. No markdown.
  * Aim for 15–40 characters. Never exceed 80.
  * Refer to the user in the second person casually ("你").
  * Include the title's essence and, if pertinent, one most important
    context detail (participant / location / note). Ignore everything
    else — the caller will append checklist / URL / attendees separately.
  * Do not restate the time; the message is delivered exactly on time,
    so words like "现在"/"该"/"到点了"/"time to" are fine but exact hours
    are noise.
  * No hedging ("may want to"), no apologies. Direct and warm.

Examples (input title -> output line):
  '睡觉' -> '该睡觉了。'
  '超市购物' -> '该去超市了。'
  '会议准备' -> '该开始准备会议了。'
  '取快递' with note 另一个包裹在保安室 -> '去取快递吧，另一个包裹在保安室。'
  '设计评审' with Bob, Alice -> '你和 Bob、Alice 的设计评审开始了。'

Return only the sentence, no quotes."""

_SYSTEM_PRE = """You rewrite an upcoming reminder into ONE natural sentence
that a friendly assistant would send N minutes BEFORE the task starts.
Match the user's language.

You MUST return exactly one sentence — never an empty string, never a
question. If the input is a bare title with no context, still produce a
natural time-plus-action sentence using ONLY what's given.

Rules:
  * ONE line, no line breaks, no emoji, no markdown.
  * 15–40 characters, never over 80.
  * Say roughly how much time is left (use minutes_left, natural
    phrasings like "还有半小时" / "10 分钟后").
  * Nudge the user toward the upcoming action; if pending_prep_items > 0,
    remind them to prepare, without listing items (caller appends list).
  * Warm and direct, second person casual ("你").

Examples (title / minutes_left -> line):
  '睡觉' / 30 -> '还有半小时该睡觉了。'
  '设计评审' / 30 with 2 pending_prep_items -> '设计评审还有半小时，准备一下吧。'
  '会议准备' / 15 -> '15 分钟后要开会，抓紧准备。'
  '取快递' / 10 -> '10 分钟后记得去取快递。'

Return only the sentence, no quotes."""


def _hermes_home_env_key() -> Optional[str]:
    """Read DEEPSEEK_API_KEY from HERMES_HOME/.env if not already in the env.

    The cron sandbox strips provider API keys from the subprocess env, so a
    no_agent script that wants to reach a provider must load them from disk.
    """
    from pathlib import Path
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
    env_path = home / ".env"
    if not env_path.exists():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "DEEPSEEK_API_KEY":
                return v.strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _first_context_hint(task: dict) -> str:
    """Pick the single most useful context detail to hand the LLM."""
    notes = (task.get("notes") or "").strip()
    if notes:
        return f"note: {notes[:60]}"
    attendees = task.get("attendees") or []
    if attendees:
        return f"with: {', '.join(attendees[:3])}"
    location = (task.get("location") or "").strip()
    if location:
        return f"at: {location}"
    url = (task.get("url") or "").strip()
    if url:
        return f"link: yes"
    return ""


def _sanitize_line(text: str, max_chars: int) -> str:
    """Strip quotes, collapse whitespace, trim to max_chars on a word/char
    boundary. Ensures a single-line output."""
    if not text:
        return ""
    # First newline wins — LLM sometimes bleeds a second line.
    text = text.splitlines()[0].strip()
    # Strip wrapping quotes.
    for pair in ('""', "''", "``"):
        if len(text) >= 2 and text[0] == pair[0] and text[-1] == pair[1]:
            text = text[1:-1].strip()
    # Collapse consecutive whitespace.
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def narrate_reminder(
    task: dict,
    *,
    mode: str = "main",
    minutes_left: int = 0,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout_s: Optional[float] = None,
    max_chars: Optional[int] = None,
) -> Optional[str]:
    """Return a single-line narrated reminder or None if the LLM fails."""
    if not isinstance(task, dict):
        return None
    title = (task.get("title") or "").strip()
    if not title:
        return None

    model = (model or os.environ.get("REMINDER_NARRATOR_MODEL") or _DEFAULT_MODEL).strip()
    base_url = (base_url or os.environ.get("REMINDER_NARRATOR_BASE_URL") or _DEFAULT_BASE_URL).strip()
    api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or _hermes_home_env_key() or ""
    timeout_s = timeout_s if timeout_s is not None else float(
        os.environ.get("REMINDER_NARRATOR_TIMEOUT") or _DEFAULT_TIMEOUT_S
    )
    max_chars = int(max_chars if max_chars is not None else
                    os.environ.get("REMINDER_NARRATOR_MAX_CHARS") or
                    _DEFAULT_MAX_CHARS)

    if not api_key and _llm_call_impl is None:
        return None  # silently fall back to static template

    checklist_pending = sum(1 for it in (task.get("checklist") or [])
                            if not it.get("done"))
    user_prompt_parts = [f"title: {title!r}"]
    if mode == "pre":
        # Deliberately keep pre-mode prompt minimal: only title + minutes_left
        # (+ pending flag). Context like attendees/URL is redundant here (the
        # surfacer appends it below), and giving it to deepseek-v4-flash for
        # pre-mode reliably triggers a runaway internal chain-of-thought that
        # exhausts max_tokens before emitting any visible sentence.
        user_prompt_parts.append(f"minutes_left: {int(minutes_left)}")
        if checklist_pending:
            user_prompt_parts.append(f"pending_prep_items: {checklist_pending}")
    else:
        context_hint = _first_context_hint(task)
        if context_hint:
            user_prompt_parts.append(context_hint)
    system = _SYSTEM_PRE if mode == "pre" else _SYSTEM_MAIN
    user = "\n".join(user_prompt_parts)

    call = _llm_call_impl or _default_llm_call
    def _one_call(u: str) -> str:
        return call(system, u, model, base_url, api_key, timeout_s)

    try:
        raw = _one_call(user)
    except Exception as exc:
        logger.warning("reminder_narrator: LLM call failed model=%s: %s", model, exc)
        return None

    line = _sanitize_line(raw, max_chars)
    if not line:
        # DeepSeek occasionally answers with an empty string when it decides
        # the task 'has nothing to say'. Retry once with an explicit prompt
        # nudge — the system already requires a non-empty sentence but the
        # extra user-side reminder appears to break the pattern reliably.
        retry_user = user + "\nreminder: your previous reply was empty. Return ONE natural sentence, in the user's language, obeying every rule."
        try:
            raw2 = _one_call(retry_user)
        except Exception as exc:
            logger.warning("reminder_narrator: retry LLM call failed: %s", exc)
            return None
        line = _sanitize_line(raw2, max_chars)
    return line or None
