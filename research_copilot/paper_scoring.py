"""Deterministic scoring for daily-paper-pick candidates.

Pure functions with no I/O and no LLM calls. The purpose is to move
score arithmetic + 30-day dedup + rank-and-filter out of the LLM's
head, where it hallucinates numeric operations, into Python where the
formulas are testable and reproducible.

Downstream: research_copilot.paper_tools wraps rank_top_k as a tool
the daily-paper-pick agent calls, so the LLM only has to make a
semantic call ("of these top-20 which is worth writing a brief about")
instead of manually scoring hundreds of candidates.

Score dimensions (all normalised to 0..1):

  relevance          keyword overlap between candidate title/summary
                     and any ACTIVE topic's include list; the higher
                     of that or any candidate.topics[].confidence
                     stamped by the fetcher.
  open_question_match how many of the profile's long_term_agenda
                     open_questions the candidate text pattern-matches;
                     capped at 3 matches -> 1.0.
  novelty            1.0 if not recommended in the last 30 days
                     (by id), else 0.0 (also acts as a hard exclude
                     in rank_top_k).
  source_tier        candidate.source_tier if stamped, else looked up
                     by candidate.sources[].name via _DEFAULT_SOURCE_TIERS
                     + caller-supplied overrides.
  profile_role       priority of the candidate's declared matched topic
                     from topics.json.
  actionability      structural bonus: has arxiv_id (+0.5), has url
                     (+0.3), mentions github/code (+0.2).

Final score = sum(dims[k] * weights[k] for k in weights).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional


# ── Source tier defaults ───────────────────────────────────────────────
# Order mirrors the SKILL.md Source Tiers table; overrides can add per-
# registry entries via rank_top_k(tier_overrides={...}).
_DEFAULT_SOURCE_TIERS: dict[str, float] = {
    "hf_daily": 1.0,
    "huggingface_daily": 1.0,
    "newsletter": 0.9,
    "newsletters": 0.9,
    "semantic_scholar": 0.85,
    "alphaxiv": 0.85,
    "github_trending": 0.80,
    "source_registry_search": 0.75,
    "source_registry_feed": 0.75,
    "tavily": 0.70,
    "arxiv_api_fallback": 0.50,
}


# ── helpers ────────────────────────────────────────────────────────────

def _text_bag(candidate: dict) -> str:
    """Concatenate searchable text (title + summary + authors), lowered."""
    parts = [
        candidate.get("title") or "",
        candidate.get("summary") or "",
        " ".join(str(a) for a in (candidate.get("authors") or [])),
    ]
    return " ".join(parts).lower()


def _keyword_overlap(text: str, terms: Iterable[str]) -> tuple[int, int]:
    """Return (matched_count, total_non_empty_terms)."""
    total = 0
    matched = 0
    for t in terms or []:
        t = (t or "").strip().lower()
        if not t:
            continue
        total += 1
        if t in text:
            matched += 1
    return matched, total


# ── dimension scorers ─────────────────────────────────────────────────

def resolve_source_tier(
    candidate: dict, tier_overrides: Optional[dict] = None
) -> float:
    """Highest tier across candidate.sources, or candidate.source_tier if set."""
    st = candidate.get("source_tier")
    if isinstance(st, (int, float)):
        return max(0.0, min(1.0, float(st)))

    tiers = dict(_DEFAULT_SOURCE_TIERS)
    if tier_overrides:
        tiers.update({str(k).lower(): float(v) for k, v in tier_overrides.items()})

    best = 0.0
    for s in candidate.get("sources") or []:
        name = (s.get("name") if isinstance(s, dict) else s) or ""
        val = tiers.get(str(name).lower())
        if val and val > best:
            best = val
    return best


def score_relevance(candidate: dict, topics: list) -> float:
    """Keyword overlap between candidate text and any ACTIVE topic's include list."""
    text = _text_bag(candidate)
    best = 0.0
    for t in topics or []:
        if (t.get("status") or "active").lower() != "active":
            continue
        include = t.get("include") or []
        matched, total = _keyword_overlap(text, include)
        if total > 0:
            score = matched / total
            if score > best:
                best = score
    # Fetcher-stamped topic confidence can override / supplement include-match.
    for tm in candidate.get("topics") or []:
        conf = tm.get("confidence") if isinstance(tm, dict) else None
        if isinstance(conf, (int, float)) and conf > best:
            best = float(conf)
    return min(1.0, best)


def score_open_question_match(
    candidate: dict, research_profile: dict
) -> float:
    """Fraction of agenda open_questions matched by candidate text (cap at 3)."""
    text = _text_bag(candidate)
    matched = 0
    for agenda in research_profile.get("long_term_agenda") or []:
        for q in agenda.get("open_questions") or []:
            tokens = re.findall(r"[A-Za-z0-9一-鿿]+", (q or "").lower())
            keywords = [w for w in tokens if len(w) > 3][:6]
            if not keywords:
                continue
            hits = sum(1 for w in keywords if w in text)
            if hits >= 2:
                matched += 1
                if matched >= 3:
                    return 1.0
    return min(1.0, matched / 3.0)


def score_novelty(
    candidate: dict,
    recommendations: list,
    now: Optional[datetime] = None,
    days: int = 30,
) -> float:
    """1.0 if not recommended within the last N days by id, else 0.0."""
    if now is None:
        now = datetime.now(timezone.utc)
    cid = candidate.get("id") or candidate.get("arxiv_id") or ""
    if not cid:
        return 1.0
    cutoff = now - timedelta(days=days)
    for r in recommendations or []:
        rid = r.get("id") or r.get("arxiv_id") or ""
        if rid != cid:
            continue
        stamp = (
            r.get("recommended_at")
            or r.get("delivered_at")
            or r.get("created_at")
        )
        if not stamp:
            return 0.0
        try:
            dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except Exception:
            return 0.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= cutoff:
            return 0.0
    return 1.0


def score_profile_role(candidate: dict, topics: list) -> float:
    """Priority of the candidate's first declared matched topic."""
    matches = candidate.get("topics") or []
    if not matches:
        return 0.0
    first_name = (
        matches[0].get("name") if isinstance(matches[0], dict) else str(matches[0])
    )
    for t in topics or []:
        if (t.get("name") or "") == first_name:
            prio = t.get("priority")
            if isinstance(prio, (int, float)):
                return max(0.0, min(1.0, float(prio)))
    return 0.0


def score_actionability(candidate: dict) -> float:
    """Structural bonus for having actionable artifacts."""
    score = 0.0
    if candidate.get("arxiv_id"):
        score += 0.5
    if candidate.get("url"):
        score += 0.3
    text = _text_bag(candidate)
    if "github.com" in text or "code" in text:
        score += 0.2
    return min(1.0, score)


# ── composed scorers ──────────────────────────────────────────────────

def score_candidate(
    candidate: dict,
    *,
    weights: dict,
    topics: list,
    research_profile: dict,
    recommendations: list,
    tier_overrides: Optional[dict] = None,
    now: Optional[datetime] = None,
    novelty_days: int = 30,
) -> dict:
    """Return {score, dims, reasons} for a single candidate."""
    dims = {
        "relevance": score_relevance(candidate, topics),
        "open_question_match": score_open_question_match(candidate, research_profile),
        "novelty": score_novelty(candidate, recommendations, now=now, days=novelty_days),
        "source_tier": resolve_source_tier(candidate, tier_overrides),
        "profile_role": score_profile_role(candidate, topics),
        "actionability": score_actionability(candidate),
    }
    total = 0.0
    parts = []
    for k, w in (weights or {}).items():
        v = dims.get(k, 0.0)
        try:
            w = float(w)
        except Exception:
            continue
        total += w * v
        parts.append(f"{k}={v:.2f}(w={w:.2f})")
    return {
        "score": round(total, 4),
        "dims": dims,
        "reasons": " · ".join(parts),
    }


def rank_top_k(
    candidates: list,
    *,
    weights: dict,
    topics: list,
    research_profile: dict,
    recommendations: list,
    threshold: float = 0.65,
    k: int = 20,
    tier_overrides: Optional[dict] = None,
    now: Optional[datetime] = None,
    include_below_threshold: bool = True,
    require_candidate_status: bool = True,
    novelty_days: int = 30,
) -> list[dict]:
    """Filter, score, sort. Return list of {score, dims, reasons, candidate}.

    Filters (in order):
    * Candidates whose status != 'candidate' when require_candidate_status.
    * Candidates recommended in the last novelty_days days (hard exclude).
    * (Optional) score < threshold when include_below_threshold=False.

    By default keeps items below threshold so the agent can inspect them
    and decide [SILENT] itself — this keeps the tool's behaviour predictable
    and easy to reason about.
    """
    scored = []
    for c in candidates or []:
        status = c.get("status") or "candidate"
        if require_candidate_status and status != "candidate":
            continue
        result = score_candidate(
            c,
            weights=weights,
            topics=topics,
            research_profile=research_profile,
            recommendations=recommendations,
            tier_overrides=tier_overrides,
            now=now,
            novelty_days=novelty_days,
        )
        if result["dims"]["novelty"] == 0.0:
            continue
        if not include_below_threshold and result["score"] < threshold:
            continue
        scored.append({**result, "candidate": c})
    scored.sort(
        key=lambda x: (
            x["score"],
            x.get("candidate", {}).get("published", ""),
        ),
        reverse=True,
    )
    return scored[:k]
