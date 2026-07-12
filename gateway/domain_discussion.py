"""Domain-scoped Q&A / discussion for commands-only Weixin bot.

Three constrained assistants — ``paper``, ``s`` (schedule/tasks), and ``th``
(thoughts/ideas) — that answer domain-scoped questions without opening free
chat. Each assistant:

* Reads only the files under its own domain (data pre-loaded into the system
  prompt; the constrained agent runs with **no tools**).
* Uses ``session_db=None`` / ``skip_memory=True`` / ``skip_context_files=True``
  so its inputs and outputs never touch the main gateway transcript.
* Runs a single model turn (``max_iterations=1``) with a bounded ``max_tokens``.
* Persists an audit entry to ``<HERMES_HOME>/discussions.jsonl``.

Phase B — sticky discussions:

* ``open_active_discussion(domain, subject_id?)`` writes
  ``<HERMES_HOME>/active_discussion.json`` with a TTL. While that file is
  present and unexpired, plain-text messages route to that domain's ``ask``
  handler instead of the LLM command router.
* ``clear_active_discussion()`` ends the sticky session (also happens on
  end-intent phrases or the ``/end`` command).

Safety model:

The constrained agent has no shell/file-write/network tools, and its system
prompt forbids topics outside its domain. It also runs in a fresh
``AIAgent(...)`` with ``session_db=None`` so a compromised prompt cannot leak
into the main conversation.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("gateway.domain_discussion")


DEFAULT_ASK_TTL_MINUTES = 15
DEFAULT_DISCUSS_TTL_MINUTES = 30
MAX_TURNS_KEPT = 20

_END_INTENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/?end\s*$", re.IGNORECASE),
    re.compile(r"^(stop|exit|quit|nevermind|never mind|done)\s*$", re.IGNORECASE),
    re.compile(r"^(算了|结束|结束吧|退出|退出讨论|停止|停一下|好了|不聊了|不用了|先这样|够了)\s*[。.!！]?$"),
)

DOMAINS: tuple[str, ...] = ("paper", "s", "th")

DOMAIN_LABELS: Dict[str, str] = {
    "paper": "Research Copilot 论文/研究助手",
    "s": "日程与提醒助手",
    "th": "想法与思考助手",
}

DOMAIN_SCOPE_DESC: Dict[str, str] = {
    "paper": (
        "只讨论用户的科研数据：研究画像 (research_profile)、关注主题 (topics)、"
        "论文源 (sources)、推荐历史 (recommendations)、保存/读过的论文 (saves)、"
        "以及用户反馈 (interactions)。不要讨论论文数据以外的话题。"
    ),
    "s": (
        "只讨论用户的日程与提醒任务：当前活跃的 tasks、最近完成/归档的 tasks、"
        "触发时间与备注。可以提出对现有安排的建议、检查冲突、做归纳。"
        "不要建议或声称已经创建/修改/删除任务；如果用户希望改动，请引导他们"
        "发送 `/s add`、`/s done <id>`、`/s rm <id>` 等命令自行完成。"
    ),
    "th": (
        "只讨论用户的想法 (thoughts) 与思维备忘：当前活跃、休眠、以及最近归档的 thoughts。"
        "可以就单个 thought 深挖、追问、跨 thought 归纳、发起思维发散，"
        "也可以主动指出与其他 thought/论文的联系并邀请用户展开。"
        "不要修改 thoughts 数据；用户可以用 `/th done`、`/th rm`、`/th pause`、`/th resume` 自行处理。"
    ),
}


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()).expanduser()


def _active_discussion_path() -> Path:
    return _hermes_home() / "active_discussion.json"


def _discussions_log_path() -> Path:
    return _hermes_home() / "discussions.jsonl"


def _load_json_or(default: Any, path: Path) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _read_jsonl_tail(path: Path, n: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if n and len(rows) > n:
        rows = rows[-n:]
    return rows


# ---------------------------------------------------------------------------
# Active-discussion state (Phase B)
# ---------------------------------------------------------------------------


def is_expired(discussion: Optional[Dict[str, Any]]) -> bool:
    if not discussion:
        return True
    expires_at = _parse_iso(discussion.get("expires_at"))
    if expires_at is None:
        return True
    return _now() >= expires_at


def load_active_discussion() -> Optional[Dict[str, Any]]:
    """Return the current active discussion, or None if none/expired.

    Corrupt or expired state is cleared as a side effect so callers can rely
    on the returned dict being live.
    """
    path = _active_discussion_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("active_discussion.json is corrupt; clearing")
        clear_active_discussion()
        return None
    if not isinstance(data, dict):
        clear_active_discussion()
        return None
    if is_expired(data):
        clear_active_discussion()
        return None
    return data


def _save_active_discussion(disc: Dict[str, Any]) -> None:
    path = _active_discussion_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(disc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def open_active_discussion(
    domain: str,
    *,
    subject_id: Optional[str] = None,
    ttl_minutes: int = DEFAULT_DISCUSS_TTL_MINUTES,
    kind: str = "discuss",
) -> Dict[str, Any]:
    """Create/refresh the active-discussion sticky state for a domain."""
    now = _now()
    disc: Dict[str, Any] = {
        "kind": kind,
        "domain": domain,
        "subject_id": subject_id,
        "opened_at": _iso(now),
        "expires_at": _iso(now + timedelta(minutes=ttl_minutes)),
        "ttl_minutes": ttl_minutes,
        "turns": [],
    }
    _save_active_discussion(disc)
    return disc


def refresh_active_discussion(
    disc: Dict[str, Any],
    *,
    ttl_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    ttl = ttl_minutes if ttl_minutes is not None else int(disc.get("ttl_minutes") or DEFAULT_DISCUSS_TTL_MINUTES)
    disc["expires_at"] = _iso(_now() + timedelta(minutes=ttl))
    disc["ttl_minutes"] = ttl
    _save_active_discussion(disc)
    return disc


def append_active_discussion_turn(
    disc: Dict[str, Any],
    user: str,
    assistant: str,
) -> Dict[str, Any]:
    turns = list(disc.get("turns") or [])
    turns.append({"user": user, "assistant": assistant, "at": _iso(_now())})
    if len(turns) > MAX_TURNS_KEPT:
        turns = turns[-MAX_TURNS_KEPT:]
    disc["turns"] = turns
    return refresh_active_discussion(disc)


def clear_active_discussion() -> bool:
    """Delete the active-discussion state. Returns True if something was cleared."""
    path = _active_discussion_path()
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def looks_like_end_intent(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    for pat in _END_INTENT_PATTERNS:
        if pat.search(t):
            return True
    return False


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def log_discussion_entry(
    domain: str,
    kind: str,
    subject_id: Optional[str],
    question: str,
    answer: str,
) -> None:
    entry = {
        "at": _iso(_now()),
        "domain": domain,
        "kind": kind,
        "subject_id": subject_id,
        "question": question,
        "answer": answer,
    }
    path = _discussions_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Failed to write discussions.jsonl: %s", exc)


# ---------------------------------------------------------------------------
# Domain context builders (data pre-loaded into the system prompt)
# ---------------------------------------------------------------------------


def build_paper_context(subject_id: Optional[str] = None) -> Dict[str, Any]:
    try:
        from research_copilot.storage import get_data_dir
    except ImportError:
        return {"error": "research_copilot.storage unavailable"}
    data_dir = get_data_dir()
    profile = _load_json_or({}, data_dir / "research_profile.json")
    topics_data = _load_json_or({"topics": []}, data_dir / "topics.json")
    sources_data = _load_json_or({"sources": []}, data_dir / "source_registry.json")
    state = _load_json_or({}, data_dir / "state.json")
    recs = _read_jsonl_tail(data_dir / "recommendations.jsonl", 20)
    all_candidates = _read_jsonl_tail(data_dir / "candidates.jsonl", 500)
    saves = [c for c in all_candidates if c.get("status") in {"saved", "read"}][-30:]
    interactions = _read_jsonl_tail(data_dir / "interactions.jsonl", 30)
    ctx: Dict[str, Any] = {
        "research_profile": profile,
        "topics": (topics_data.get("topics") or [])[:20] if isinstance(topics_data, dict) else [],
        "sources": (sources_data.get("sources") or [])[:40] if isinstance(sources_data, dict) else [],
        "state": state,
        "recent_recommendations": recs,
        "saved_or_read_items": saves,
        "recent_interactions": interactions,
    }
    if subject_id:
        subj = next((c for c in all_candidates if c.get("id") == subject_id), None)
        if subj:
            ctx["subject"] = subj
    return ctx


def build_schedule_context(subject_id: Optional[str] = None) -> Dict[str, Any]:
    store = _load_json_or({"tasks": [], "thoughts": []}, _hermes_home() / "thoughts.json")
    tasks = store.get("tasks", []) if isinstance(store, dict) else []
    active = [t for t in tasks if t.get("state") == "active"]
    recent_done = sorted(
        [t for t in tasks if t.get("state") in {"done", "archived"}],
        key=lambda t: t.get("last_reminded") or t.get("created_at") or "",
        reverse=True,
    )[:20]
    ctx: Dict[str, Any] = {
        "active_tasks": active,
        "recent_done_tasks": recent_done,
    }
    if subject_id:
        subj = next((t for t in tasks if t.get("id") == subject_id), None)
        if subj:
            ctx["subject"] = subj
    return ctx


def build_thoughts_context(subject_id: Optional[str] = None) -> Dict[str, Any]:
    store = _load_json_or({"tasks": [], "thoughts": []}, _hermes_home() / "thoughts.json")
    thoughts = store.get("thoughts", []) if isinstance(store, dict) else []
    active = [t for t in thoughts if t.get("state") == "active"]
    dormant = [t for t in thoughts if t.get("state") == "dormant"][:20]
    archived = sorted(
        [t for t in thoughts if t.get("state") == "archived"],
        key=lambda t: t.get("archived_at") or t.get("created_at") or "",
        reverse=True,
    )[:10]
    ctx: Dict[str, Any] = {
        "active_thoughts": active,
        "dormant_thoughts": dormant,
        "recent_archived_thoughts": archived,
    }
    if subject_id:
        subj = next((t for t in thoughts if t.get("id") == subject_id), None)
        if subj:
            ctx["subject"] = subj
    return ctx


CONTEXT_BUILDERS = {
    "paper": build_paper_context,
    "s": build_schedule_context,
    "th": build_thoughts_context,
}


# ---------------------------------------------------------------------------
# System prompt construction
# ---------------------------------------------------------------------------


def build_domain_system_prompt(
    domain: str,
    subject_id: Optional[str],
    context: Dict[str, Any],
    prior_turns: Optional[List[Dict[str, Any]]] = None,
) -> str:
    label = DOMAIN_LABELS.get(domain, domain)
    scope = DOMAIN_SCOPE_DESC.get(domain, "")
    context_json = json.dumps(context, ensure_ascii=False, indent=2)
    subject_line = f"当前讨论对象 ID：{subject_id}\n" if subject_id else ""
    prior_block = ""
    if prior_turns:
        # Keep the most recent 6 turns to give continuity without ballooning.
        recent = prior_turns[-6:]
        rendered = "\n".join(
            f"用户: {t.get('user', '').strip()}\n助手: {t.get('assistant', '').strip()}"
            for t in recent
            if t.get("user") or t.get("assistant")
        )
        if rendered:
            prior_block = f"\n上文（近几轮）:\n{rendered}\n"
    return (
        f"你是{label}。\n"
        f"{scope}\n"
        f"{subject_line}"
        "回答要用中文、简洁、贴近数据。如果无法从下面的数据里找出答案，"
        "明确告诉用户 '在你的数据里没找到相关内容'。\n"
        "**你没有任何工具**，也**不能执行任何命令或写入任何文件**——只返回文本回答。\n"
        "如果用户偏离本域话题（比如让你写代码、聊天气、闲聊），礼貌拒绝并提示他们"
        "可用 /paper、/s、/th 等命令。\n"
        "如果你注意到跨条目的联系（比如与另一个 thought 或 paper 相关），可以指出并邀请用户展开。\n"
        f"{prior_block}\n"
        f"用户当前的{label}数据快照 (JSON):\n"
        "```json\n"
        f"{context_json}\n"
        "```\n"
    )


# ---------------------------------------------------------------------------
# Constrained agent invocation
# ---------------------------------------------------------------------------


def _run_constrained_agent(system_prompt: str, user_message: str) -> str:
    """Run a fresh AIAgent with no tools, no memory, no persistence.

    Any exception is caught and rendered as a user-facing error string so a
    router path never crashes the gateway.
    """
    try:
        from run_agent import AIAgent
        from hermes_cli.config import load_config
    except ImportError as exc:
        logger.exception("Constrained agent import failed")
        return f"❌ 助手不可用：{exc}"

    cfg = load_config() or {}
    model_cfg = cfg.get("model") or {}
    model = str(model_cfg.get("default") or "")
    provider = model_cfg.get("provider")
    base_url = model_cfg.get("base_url")

    try:
        agent = AIAgent(
            model=model,
            provider=provider,
            base_url=base_url,
            enabled_toolsets=[],
            disabled_toolsets=[],
            max_iterations=1,
            max_tokens=1500,
            quiet_mode=True,
            skip_memory=True,
            skip_context_files=True,
            session_db=None,
            ephemeral_system_prompt=system_prompt,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("Constrained agent init failed")
        return f"❌ 助手初始化失败：{exc}"

    try:
        result = agent.run_conversation(user_message, conversation_history=[])
    except Exception as exc:
        logger.exception("Constrained agent turn failed")
        return f"❌ 助手调用失败：{exc}"

    text = str((result or {}).get("final_response") or "").strip()
    return text or "（助手没有返回内容。）"


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------


def run_domain_ask(
    domain: str,
    question: str,
    *,
    subject_id: Optional[str] = None,
    prior_turns: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Single-turn constrained Q&A for a domain. Returns the answer text."""
    if domain not in CONTEXT_BUILDERS:
        return "❌ 未知的功能域。"
    question = (question or "").strip()
    if not question:
        return "请输入你想问的内容。"
    try:
        context = CONTEXT_BUILDERS[domain](subject_id)
    except Exception as exc:
        logger.exception("Failed to build context for domain=%s", domain)
        return f"❌ 无法加载数据：{exc}"
    system_prompt = build_domain_system_prompt(domain, subject_id, context, prior_turns)
    answer = _run_constrained_agent(system_prompt, question)
    log_discussion_entry(domain, "ask", subject_id, question, answer)
    return answer


def handle_domain_subcommand(domain: str, args: str) -> Optional[str]:
    """Dispatch the ``ask`` / ``discuss`` / ``end`` subcommands for a domain.

    Returns:
        - A user-facing response string when the subcommand matched.
        - ``None`` when ``args`` is empty or the first token is unrecognised,
          so the caller can fall back to its own subcommand handling.
    """
    if domain not in DOMAINS:
        return None
    args = (args or "").strip()
    if not args:
        return None
    parts = args.split(maxsplit=1)
    subcmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if subcmd == "ask":
        question = rest.strip()
        if not question:
            return "请输入你想问的内容，例如：\n/{d} ask 帮我总结我的科研关注点".format(d=domain)
        # Consult any existing sticky discussion so follow-up asks have context.
        active = load_active_discussion()
        prior_turns = None
        if active and active.get("domain") == domain:
            prior_turns = active.get("turns")
        answer = run_domain_ask(domain, question, prior_turns=prior_turns)
        disc = open_active_discussion(
            domain,
            subject_id=(active or {}).get("subject_id") if active and active.get("domain") == domain else None,
            ttl_minutes=DEFAULT_ASK_TTL_MINUTES,
            kind="ask",
        )
        append_active_discussion_turn(disc, question, answer)
        return answer

    if subcmd == "discuss":
        subject_id: Optional[str] = None
        followup = ""
        if rest:
            subject_parts = rest.split(maxsplit=1)
            candidate = subject_parts[0].strip()
            # Heuristic: treat first token as a subject id when it looks like
            # one (short, no whitespace, alphanumeric/._-). Otherwise treat the
            # whole `rest` as an inline question.
            if candidate and len(candidate) <= 64 and re.fullmatch(r"[A-Za-z0-9._-]+", candidate):
                subject_id = candidate
                followup = subject_parts[1].strip() if len(subject_parts) > 1 else ""
            else:
                followup = rest.strip()
        disc = open_active_discussion(
            domain,
            subject_id=subject_id,
            ttl_minutes=DEFAULT_DISCUSS_TTL_MINUTES,
            kind="discuss",
        )
        header_bits = [f"💬 已开启 /{domain} 讨论"]
        if subject_id:
            header_bits.append(f"（对象 ID: {subject_id}）")
        header_bits.append(
            f"，接下来 {DEFAULT_DISCUSS_TTL_MINUTES} 分钟内的普通消息会继续在这个话题里。"
            f" 想退出请发 /end。"
        )
        header = "".join(header_bits)
        if not followup:
            return header + "\n请说出你想讨论的内容。"
        answer = run_domain_ask(domain, followup, subject_id=subject_id)
        append_active_discussion_turn(disc, followup, answer)
        return f"{header}\n\n{answer}"

    if subcmd == "end":
        cleared = clear_active_discussion()
        return "✅ 已结束当前讨论。" if cleared else "（当前没有进行中的讨论。）"

    return None


def continue_active_discussion(text: str) -> Optional[str]:
    """Feed plain text into whichever domain has an active discussion.

    Returns:
        - The domain agent's answer when a discussion was active and the text
          continued it.
        - ``None`` when there is no active discussion (caller should fall back
          to the normal router/deny path).
    """
    active = load_active_discussion()
    if not active:
        return None
    domain = str(active.get("domain") or "")
    if domain not in DOMAINS:
        clear_active_discussion()
        return None
    if looks_like_end_intent(text):
        clear_active_discussion()
        return "✅ 已结束当前讨论。"
    answer = run_domain_ask(
        domain,
        text,
        subject_id=active.get("subject_id"),
        prior_turns=active.get("turns"),
    )
    append_active_discussion_turn(active, text, answer)
    return answer


__all__ = [
    "DOMAINS",
    "DEFAULT_ASK_TTL_MINUTES",
    "DEFAULT_DISCUSS_TTL_MINUTES",
    "load_active_discussion",
    "open_active_discussion",
    "clear_active_discussion",
    "append_active_discussion_turn",
    "refresh_active_discussion",
    "is_expired",
    "looks_like_end_intent",
    "log_discussion_entry",
    "build_paper_context",
    "build_schedule_context",
    "build_thoughts_context",
    "build_domain_system_prompt",
    "run_domain_ask",
    "handle_domain_subcommand",
    "continue_active_discussion",
]
