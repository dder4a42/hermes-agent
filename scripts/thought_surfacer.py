#!/usr/bin/env python3
"""Thought Surfacer — picks one active thought daily and sends it to WeChat.

Reads ``$HERMES_HOME/thoughts.json`` (defaults to ``~/.hermes/thoughts.json``),
selects an active thought (prioritising oldest that hasn't been surfaced
recently), prints it to stdout for cron delivery (no_agent=True).

Output format:
  💭 Thought Incubation
  <title>
  <summary>

Silent when no active thoughts exist.

Environment variables:
  HERMES_HOME  — profile home; defaults to ~/.hermes. Set by the gateway so
                 per-profile cron jobs read/write the correct thoughts.json.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()


def _store_path() -> Path:
    return _hermes_home() / "thoughts.json"


def _save_store(store: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    path = _store_path()
    if not path.exists():
        return

    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    active = [th for th in store.get("thoughts", []) if th.get("state") == "active"]
    if not active:
        return

    # Pick the thought with the oldest surface time (or never surfaced)
    # that also has the fewest surface_count — fair rotation.
    def _pick_key(th):
        surfaced = th.get("surfaced_at")
        if not surfaced:
            return (0, 0, th.get("created_at", ""))
        count = th.get("surface_count", 0) or 0
        return (1, count, surfaced)

    active.sort(key=_pick_key)
    chosen = active[0]

    title = chosen.get("title", "Untitled idea")
    summary = chosen.get("summary", "")
    source = chosen.get("source", "")

    lines = ["💭 **Thought Incubation**"]
    lines.append(f"**{title}**")
    if summary:
        lines.append("")
        lines.append(summary)
    if source:
        lines.append("")
        lines.append(f"*Origin: {source}*")
    lines.append("")
    lines.append("Reply to discuss, or `/th done` to archive.")

    # Mark surfaced
    chosen["surfaced_at"] = datetime.now(timezone.utc).isoformat()
    chosen["surface_count"] = (chosen.get("surface_count", 0) or 0) + 1
    _save_store(store)

    sys.stdout.write("\n".join(lines))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
