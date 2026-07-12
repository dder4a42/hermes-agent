#!/usr/bin/env python3
"""
Paper Health — Research Copilot weekly summary script.

Consumes ``interactions.jsonl`` and ``recommendations.jsonl`` from the active
profile's Research Copilot data directory and prints a human-readable weekly
summary suitable for delivery over Weixin. Never mutates state — the
proposals section is advisory only; the user applies changes explicitly via
``/paper apply <proposal_id>`` (Phase 5+).

Runs as a no-agent cron (Sat 09:00 by default) and is also invoked by
``/paper health``.

Environment variables:
  HERMES_HOME              — profile home; defaults to ~/.hermes.
  RESEARCH_COPILOT_WINDOW  — window size in days (default 7). Set 0 for "all time".
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Paths (delegated to research_copilot.storage) ────────────────────────────
try:
    from research_copilot.storage import (
        get_hermes_home,
        get_data_dir,
        read_recommendations,
    )
except ImportError:
    # Standalone-invocation fallback for cron.
    def get_hermes_home() -> Path:
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()

    def get_data_dir() -> Path:
        return get_hermes_home() / "research-copilot"

    def read_recommendations(limit: int = 100) -> list[dict]:
        path = get_data_dir() / "recommendations.jsonl"
        if not path.exists():
            return []
        records: list[dict] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records[-limit:] if limit else records


DATA_DIR = get_data_dir()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Accept both "…Z" and "…+00:00" variants.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return default


def _within_window(record: dict, cutoff: datetime | None, ts_field: str) -> bool:
    if cutoff is None:
        return True
    ts = _parse_iso(record.get(ts_field))
    return ts is not None and ts >= cutoff


def _topic_labels(candidate: dict) -> list[str]:
    topics = candidate.get("topics") or []
    labels: list[str] = []
    for t in topics:
        if isinstance(t, dict) and t.get("name"):
            labels.append(t["name"])
        elif isinstance(t, str):
            labels.append(t)
    return labels


def _source_names(candidate: dict) -> list[str]:
    sources = candidate.get("sources") or []
    names: list[str] = []
    for s in sources:
        if isinstance(s, dict) and s.get("name"):
            names.append(s["name"])
        elif isinstance(s, str):
            names.append(s)
    return names


def build_report(
    data_dir: Path | None = None,
    window_days: int = 7,
    *,
    now: datetime | None = None,
) -> str:
    """Compose the health report as a single string.

    Called directly by ``/paper health`` (via research_copilot.commands) and
    by the cron entrypoint. Never touches network state.
    """
    data_dir = data_dir or DATA_DIR
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days) if window_days > 0 else None

    state = _load_json(data_dir / "state.json", {})
    topics_data = _load_json(data_dir / "topics.json", {"topics": []})
    active_topics = [
        t for t in topics_data.get("topics", []) if t.get("status", "active") == "active"
    ]

    recs = _read_jsonl(data_dir / "recommendations.jsonl")
    interactions = _read_jsonl(data_dir / "interactions.jsonl")
    candidates = _read_jsonl(data_dir / "candidates.jsonl")

    recs_in_window = [r for r in recs if _within_window(r, cutoff, "recommended_at")]
    # Interaction records may use "at" (data-contract canonical) or
    # "created_at" (legacy from research_copilot.commands._record_action);
    # accept both, prefer "at" when present.
    def _ix_timestamp(ix: dict) -> str | None:
        return ix.get("at") or ix.get("created_at")

    def _ix_kind(ix: dict) -> str | None:
        return ix.get("kind") or ix.get("type")

    ix_in_window = [
        i for i in interactions
        if cutoff is None or (_parse_iso(_ix_timestamp(i)) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]

    kind_counts = Counter(_ix_kind(i) for i in ix_in_window)
    saved = kind_counts.get("save", 0)
    skipped = kind_counts.get("skip", 0)
    read = kind_counts.get("read", 0)
    feedback = kind_counts.get("feedback", 0)

    candidate_by_id = {c.get("id"): c for c in candidates if c.get("id")}

    # High-skip topics: skip interactions grouped by their recommendation's
    # topic_matches. Fall back to the candidate's topics[].name.
    topic_skips: Counter[str] = Counter()
    topic_saves: Counter[str] = Counter()
    for ix in ix_in_window:
        item_id = ix.get("item_id")
        if not item_id:
            continue
        rec = next((r for r in recs if r.get("id") == item_id), None)
        topic_matches = rec.get("topic_matches") if rec else None
        if not topic_matches:
            topic_matches = _topic_labels(candidate_by_id.get(item_id, {}))
        kind = _ix_kind(ix)
        for label in topic_matches:
            if kind == "skip":
                topic_skips[label] += 1
            elif kind in {"save", "read"}:
                topic_saves[label] += 1

    # Source effectiveness in the window: keyed by source name.
    source_saves: Counter[str] = Counter()
    source_skips: Counter[str] = Counter()
    for ix in ix_in_window:
        item_id = ix.get("item_id")
        if not item_id:
            continue
        kind = _ix_kind(ix)
        for src in _source_names(candidate_by_id.get(item_id, {})):
            if kind in {"save", "read"}:
                source_saves[src] += 1
            elif kind == "skip":
                source_skips[src] += 1

    # Proposals (advisory only — the user applies them via /paper apply).
    proposals: list[str] = []
    for topic, skip_count in topic_skips.most_common():
        if skip_count >= 3 and topic_saves.get(topic, 0) == 0:
            proposals.append(
                f"lower priority for topic \"{topic}\" ({skip_count} skips, 0 saves this window)"
            )
    for src, skip_count in source_skips.most_common():
        if skip_count >= 3 and source_saves.get(src, 0) == 0:
            proposals.append(
                f"lower tier for source \"{src}\" ({skip_count} skips, 0 saves this window)"
            )
    for src, save_count in source_saves.most_common(3):
        if save_count >= 2 and source_skips.get(src, 0) == 0:
            proposals.append(
                f"raise tier for source \"{src}\" ({save_count} save/read, 0 skips)"
            )

    # Compose output.
    window_label = f"last {window_days} days" if window_days > 0 else "all time"
    lines: list[str] = []
    lines.append(f"Research Copilot — Health ({window_label})")
    lines.append("")
    lines.append(f"Active topics: {len(active_topics)}")
    lines.append(f"Recommendations delivered: {len(recs_in_window)}")
    lines.append(
        f"Feedback: {saved} save · {read} read · {skipped} skip · {feedback} note"
    )
    lines.append(f"Last fetch: {state.get('last_fetch_at', 'never')}")
    lines.append(f"Last pick: {state.get('last_recommendation_at', 'never')}")

    no_pick_days = int(state.get("consecutive_no_pick_days", 0) or 0)
    if no_pick_days >= 3:
        lines.append(f"WARNING: no pick for {no_pick_days} days in a row.")

    if topic_saves or topic_skips:
        lines.append("")
        lines.append("Topic engagement:")
        seen_topics: set[str] = set()
        for topic, count in topic_saves.most_common(5):
            skips = topic_skips.get(topic, 0)
            lines.append(f"  • {topic}: {count} save/read, {skips} skip")
            seen_topics.add(topic)
        for topic, count in topic_skips.most_common(5):
            if topic in seen_topics:
                continue
            lines.append(f"  • {topic}: 0 save/read, {count} skip")

    if source_saves or source_skips:
        lines.append("")
        lines.append("Source engagement:")
        seen_sources: set[str] = set()
        for src, count in source_saves.most_common(5):
            skips = source_skips.get(src, 0)
            lines.append(f"  • {src}: {count} save/read, {skips} skip")
            seen_sources.add(src)
        for src, count in source_skips.most_common(5):
            if src in seen_sources:
                continue
            lines.append(f"  • {src}: 0 save/read, {count} skip")

    if proposals:
        lines.append("")
        lines.append("Suggested adjustments (advisory — not applied):")
        for p in proposals:
            lines.append(f"  • {p}")
        lines.append("")
        lines.append("Use `/paper apply <id>` to apply, `/paper reject <id>` to reject.")
    elif recs_in_window or ix_in_window:
        lines.append("")
        lines.append("No adjustments suggested — signal looks balanced.")

    return "\n".join(lines).rstrip() + "\n"


def _window_from_env() -> int:
    raw = os.environ.get("RESEARCH_COPILOT_WINDOW", "7")
    try:
        return max(int(raw), 0)
    except ValueError:
        return 7


def main() -> int:
    report = build_report(window_days=_window_from_env())
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
