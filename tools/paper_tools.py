"""Hermes agent tools for daily-paper-pick.

Three tools that let the daily-paper-pick agent skip manual arithmetic
over hundreds of candidates and instead:

  paper_top_candidates(k)              — Python-scored top-K candidates,
                                          filtered by 30-day novelty,
                                          sorted by combined score.
  paper_recent_recommendations(days)   — sanity list of what was pushed
                                          recently, keyed by item id.
  paper_write_recommendation(item_id,  — validate + append to
      brief_text, signal_roles, notes)   recommendations.jsonl, flip
                                          candidates.jsonl status,
                                          update state.json.

The tools are registered under toolset ``paper`` so the daily-paper-pick
cron job can enable them without exposing them elsewhere. All paths are
resolved through research_copilot.storage helpers so we stay profile-
safe.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from research_copilot import paper_scoring
from research_copilot import storage
from tools.registry import registry


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "+00:00"
    )


def _err(msg: str, **extra) -> str:
    return json.dumps({"success": False, "error": msg, **extra}, ensure_ascii=False)


def _ok(**extra) -> str:
    return json.dumps({"success": True, **extra}, ensure_ascii=False)


# ── paper_top_candidates ───────────────────────────────────────────────

def _shrink_summary(text: Any, limit: int = 400) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _compact_candidate(c: dict) -> dict:
    """Return a minimal candidate dict for tool response (keep context small)."""
    return {
        "id": c.get("id") or c.get("arxiv_id") or "",
        "title": c.get("title") or "",
        "authors": (c.get("authors") or [])[:5],
        "url": c.get("url") or "",
        "arxiv_id": c.get("arxiv_id") or "",
        "published": c.get("published") or "",
        "summary": _shrink_summary(c.get("summary")),
        "sources": [
            {"name": (s.get("name") if isinstance(s, dict) else s) or ""}
            for s in (c.get("sources") or [])[:3]
        ],
        "topics": [
            {"name": (t.get("name") if isinstance(t, dict) else t) or ""}
            for t in (c.get("topics") or [])[:3]
        ],
    }


def _handler_paper_top_candidates(args: dict, task_id: str = None) -> str:
    """Return Python-scored top-K candidates in ranked order."""
    try:
        k = int(args.get("k") or 20)
    except (TypeError, ValueError):
        k = 20
    k = max(1, min(50, k))

    try:
        data_dir = storage.get_data_dir()
        topics_doc = storage.load_topics()
        topics = topics_doc.get("topics") or []
        config = storage.load_config()
        weights = (config.get("score_weights") or {})
        threshold = float(config.get("threshold") or 0.65)

        rp_path = data_dir / "research_profile.json"
        research_profile = {}
        if rp_path.exists():
            try:
                research_profile = json.loads(rp_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                research_profile = {}

        # Full history for accurate 30-day novelty dedup; storage.read_recommendations
        # has a small limit by default.
        recs = storage._read_jsonl(data_dir / "recommendations.jsonl")
        candidates = storage._read_jsonl(data_dir / "candidates.jsonl")

        ranked = paper_scoring.rank_top_k(
            candidates,
            weights=weights,
            topics=topics,
            research_profile=research_profile,
            recommendations=recs,
            threshold=threshold,
            k=k,
        )
    except Exception as exc:  # keep the tool robust — never crash the agent
        return _err(f"top_candidates failed: {type(exc).__name__}: {exc}")

    payload = []
    for row in ranked:
        payload.append(
            {
                "score": row["score"],
                "dims": row["dims"],
                "reasons": row["reasons"],
                "candidate": _compact_candidate(row["candidate"]),
            }
        )
    return _ok(
        k=k,
        threshold=threshold,
        weights=weights,
        total_ranked=len(payload),
        items=payload,
    )


# ── paper_recent_recommendations ───────────────────────────────────────

def _handler_paper_recent_recommendations(args: dict, task_id: str = None) -> str:
    try:
        days = int(args.get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(365, days))
    try:
        limit = int(args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(500, limit))

    try:
        data_dir = storage.get_data_dir()
        rows = storage._read_jsonl(data_dir / "recommendations.jsonl")
    except Exception as exc:
        return _err(f"read failed: {type(exc).__name__}: {exc}")

    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - days * 86400
    recent = []
    for r in rows:
        stamp = (
            r.get("recommended_at")
            or r.get("delivered_at")
            or r.get("created_at")
        )
        if stamp:
            try:
                dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt.timestamp() < cutoff:
                    continue
            except Exception:
                pass  # missing / unparseable stamp — include to be safe
        recent.append(
            {
                "id": r.get("id") or r.get("arxiv_id") or "",
                "title": r.get("title") or "",
                "recommended_at": stamp,
                "score": r.get("score"),
                "topic_matches": r.get("topic_matches") or [],
            }
        )
    recent.sort(key=lambda r: str(r.get("recommended_at") or ""), reverse=True)
    return _ok(days=days, count=len(recent), items=recent[:limit])


# ── paper_write_recommendation ─────────────────────────────────────────

def _handler_paper_write_recommendation(args: dict, task_id: str = None) -> str:
    item_id = str(args.get("item_id") or "").strip()
    brief_text = str(args.get("brief_text") or "").strip()
    signal_roles = args.get("signal_roles") or []
    notes = str(args.get("notes") or "").strip()

    if not item_id:
        return _err("item_id is required")
    if not brief_text:
        return _err("brief_text is required")
    if isinstance(signal_roles, str):
        signal_roles = [signal_roles]
    if not isinstance(signal_roles, list):
        signal_roles = []

    try:
        data_dir = storage.ensure_data_dir()
        candidates = storage._read_jsonl(data_dir / "candidates.jsonl")
        # Validate the item_id.
        target = None
        for c in candidates:
            cid = c.get("id") or c.get("arxiv_id") or ""
            if cid == item_id:
                target = c
                break
        if target is None:
            return _err(
                f"item_id {item_id!r} not found in candidates.jsonl",
                hint=(
                    "Only ids returned by paper_top_candidates are valid. "
                    "Do not invent ids."
                ),
            )
        if (target.get("status") or "candidate") != "candidate":
            return _err(
                f"item_id {item_id!r} has status "
                f"{target.get('status')!r}; already actioned"
            )

        # Recent dedup — protect against LLM re-picking a paper already in
        # the 30-day window.
        recs = storage._read_jsonl(data_dir / "recommendations.jsonl")
        novelty = paper_scoring.score_novelty(target, recs)
        if novelty == 0.0:
            return _err(
                f"item_id {item_id!r} was recommended in the last 30 days; "
                "skip or pick a different candidate"
            )

        # Build the recommendation record. Schema matches storage
        # find_recommendation() readers.
        stamp = _now_iso()
        score = args.get("score")
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None

        rec = {
            "id": item_id,
            "type": target.get("type") or "paper",
            "title": target.get("title") or "",
            "authors": target.get("authors") or [],
            "url": target.get("url") or "",
            "arxiv_id": target.get("arxiv_id") or "",
            "summary": target.get("summary") or "",
            "recommended_at": stamp,
            "score": score,
            "signal_roles": signal_roles,
            "topic_matches": [
                (t.get("name") if isinstance(t, dict) else str(t))
                for t in (target.get("topics") or [])
            ],
            "open_questions_addressed": args.get("open_questions_addressed") or [],
            "brief_text": brief_text,
            "notes": notes,
        }

        # Append recommendation atomically.
        rec_path = data_dir / "recommendations.jsonl"
        with rec_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

        # Flip candidate status.
        for c in candidates:
            cid = c.get("id") or c.get("arxiv_id") or ""
            if cid == item_id:
                c["status"] = "recommended"
                c["scored"] = True
                c["recommended_at"] = stamp
                break
        storage._write_jsonl(data_dir / "candidates.jsonl", candidates)

        # Update state.json.
        state_path = data_dir / "state.json"
        state = storage._read_json(state_path, {}) or {}
        state["last_pick_id"] = item_id
        state["last_recommendation_at"] = stamp[:10]
        state["consecutive_no_pick_days"] = 0
        storage._write_json(state_path, state)
    except Exception as exc:
        return _err(f"write failed: {type(exc).__name__}: {exc}")

    return _ok(item_id=item_id, recommended_at=stamp, score=score)


# ── Registry ───────────────────────────────────────────────────────────

def _make_handler(fn):
    return lambda args, **kw: fn(args, task_id=kw.get("task_id"))


def _always() -> bool:
    return True


registry.register(
    name="paper_top_candidates",
    toolset="paper",
    schema={
        "name": "paper_top_candidates",
        "description": (
            "Return Python-scored top-K daily-paper-pick candidates (sorted "
            "by combined score, filtered to status='candidate' and NOT "
            "recommended in the last 30 days). Use this INSTEAD of reading "
            "candidates.jsonl manually; the scoring already applies "
            "config.json weights and threshold. Each item includes score, "
            "dims (relevance, open_question_match, novelty, source_tier, "
            "profile_role, actionability), and a short reasons string."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "k": {
                    "type": "integer",
                    "description": "How many top candidates to return (max 50, default 20).",
                }
            },
            "required": [],
        },
    },
    handler=_make_handler(_handler_paper_top_candidates),
    check_fn=_always,
)

registry.register(
    name="paper_recent_recommendations",
    toolset="paper",
    schema={
        "name": "paper_recent_recommendations",
        "description": (
            "List recommendations from the last N days (default 30). Use "
            "this as a sanity check before writing a new recommendation. "
            "Do NOT read recommendations.jsonl manually."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Look-back window in days (max 365, default 30).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries returned (default 50).",
                },
            },
            "required": [],
        },
    },
    handler=_make_handler(_handler_paper_recent_recommendations),
    check_fn=_always,
)

registry.register(
    name="paper_write_recommendation",
    toolset="paper",
    schema={
        "name": "paper_write_recommendation",
        "description": (
            "Persist a chosen paper recommendation. Python validates "
            "item_id (must exist in candidates.jsonl with status='candidate' "
            "and not already recommended in the last 30 days), then appends "
            "to recommendations.jsonl, flips the candidate's status to "
            "'recommended', and updates state.json. Call this AFTER "
            "writing the brief; do NOT edit recommendations.jsonl directly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": (
                        "Candidate id from paper_top_candidates. Must exist in "
                        "candidates.jsonl."
                    ),
                },
                "brief_text": {
                    "type": "string",
                    "description": (
                        "The Chinese-language brief text you produced per "
                        "references/format.md. This is what gets delivered "
                        "to WeChat."
                    ),
                },
                "signal_roles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "One or more of: Evidence update, Belief challenge, "
                        "Gap filler, Trend signal, Tool useful."
                    ),
                },
                "score": {
                    "type": "number",
                    "description": (
                        "Combined score from paper_top_candidates for this "
                        "id (optional but useful for audit)."
                    ),
                },
                "open_questions_addressed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "IDs or one-line texts of open_questions this paper "
                        "advances (from research_profile.json)."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": "Optional internal notes.",
                },
            },
            "required": ["item_id", "brief_text"],
        },
    },
    handler=_make_handler(_handler_paper_write_recommendation),
    check_fn=_always,
)
