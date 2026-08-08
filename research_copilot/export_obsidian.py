"""Export the Research Copilot library to an Obsidian vault as paper notes.

Design
------
- One markdown note per research_item under ``Research Library/01 - Papers/``
  (news items under ``02 - News/``), named ``YYYY-MM-DD - <slug>.md``.
- YAML frontmatter carries metadata (arxiv_id, url, authors, topics, status,
  score, source, ...) so Dataview / search can query it.
- The note body has a summary block plus a seven-part analysis area
  (背景/现象/观点/方法/实验设计/观察结论/局限) that gets backfilled from
  deep-research reports in ``reports/`` when a matching analysis exists.
- Idempotent: the same ``canonical_key`` always maps to the same filename,
  so re-exporting updates in place instead of duplicating.
- ``00 - Index.md`` is regenerated as a Map-of-Content (by topic + status).

Usage
-----
    python -m research_copilot.export_obsidian --vault /path/to/vault [--full]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Old topic ids seen in historical item_topics rows -> current active ids.
TOPIC_ID_MAP = {
    "research-agent": "search-agent",
    "multimodal-lmm": "mllm",
    "evaluation-harness": "search-agent",
    "agent-infra": "long-horizon-agent",
    "inference": "long-horizon-agent",
}

SEVEN_PARTS = ["背景", "现象", "观点", "方法", "实验设计", "观察结论", "局限"]

DEFAULT_VAULT = Path.home() / "Documents" / "Obsidian Vault"
LIBRARY_SUBDIR = "Research Library"


def _slug(title: str, max_len: int = 220) -> str:
    """Filesystem-safe slug for note filenames.

    Must match the deep-research agent's filename convention (no truncation
    at 64 chars — that created duplicate notes: agent wrote the full-slug
    note, the exporter wrote a truncated-slug scaffold for the same paper).
    """
    s = re.sub(r"[^\w\u4e00-\u9fff -]", " ", title, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s.strip())
    s = s.strip("-")
    return s[:max_len].rstrip("-") or "untitled"


def _extract_arxiv_id(item: dict[str, Any]) -> str:
    key = item.get("canonical_key") or ""
    if key.startswith("arxiv:"):
        return key.split(":", 1)[1]
    url = item.get("url") or ""
    m = re.search(r"(?:arxiv\.org/abs/|huggingface\.co/papers/)([\w.-]+)", url)
    return m.group(1) if m else ""


def _date(dt: str | None) -> str:
    if not dt:
        return ""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", dt)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", dt)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return dt[:10]


def _read_topics(conn: sqlite3.Connection, item_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT topic_id FROM item_topics WHERE item_id=?", (item_id,)
    ).fetchall()
    seen: list[str] = []
    for row in rows:
        tid = str(row[0] or "")
        if not tid:
            continue
        mapped = TOPIC_ID_MAP.get(tid, tid)
        if mapped not in seen:
            seen.append(mapped)
    return seen


def _read_sources(conn: sqlite3.Connection, item_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT source_id FROM item_sources WHERE item_id=?", (item_id,)
    ).fetchall()
    return [r[0] for r in rows]


def _read_recommendation(conn: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT score, recommended_at, rationale FROM recommendations "
        "WHERE item_id=? ORDER BY recommended_at DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "score": round(row[0], 2) if row[0] is not None else None,
        "recommended_at": _date(row[1]),
        "rationale": row[2] or "",
    }


def _frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    for key in ("type", "title", "arxiv_id", "url", "authors", "published",
                "discovered", "topics", "status", "source", "score",
                "recommended", "tags"):
        value = fields.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            rendered = "[" + ", ".join(f'"{v}"' for v in value) + "]"
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = f'"{str(value).replace(chr(34), chr(39))}"'
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines)


def _has_analysis(path: Path) -> bool:
    """True if a note already carries filled-in seven-part analysis (written
    by the deep-research agent), so the exporter should not overwrite it."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    import re as _re
    return any(
        bool(_re.search(rf"\*\*{part}\*\*：[^\n]*\S", text))
        for part in SEVEN_PARTS
    )


def _summary_block(item: dict[str, Any]) -> str:
    summary = (item.get("summary") or "").strip()
    if not summary:
        return ""
    return f"> {summary}\n"


def _analysis_section() -> str:
    """Seven-part analysis scaffold, backfilled by _backfill_analysis()."""
    lines = ["## 解读", ""]
    for part in SEVEN_PARTS:
        lines.append(f"**{part}**：")
        lines.append("")
    return "\n".join(lines)


def _note_filename(item: dict[str, Any]) -> str:
    date = _date(item.get("published_at") or item.get("first_discovered_at"))
    prefix = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{prefix} - {_slug(item.get('title') or 'untitled')}.md"


def _related_wikilinks(conn: sqlite3.Connection, canonical_key: str,
                       note_map: dict[str, str]) -> str:
    """Link other notes that share a topic with this item (cheap related-papers)."""
    return ""  # enriched by caller via note_map; kept simple for now


def _parse_report_analyses(reports_dir: Path) -> list[dict[str, Any]]:
    """Parse seven-part analyses out of deep-research report markdown files.

    Line-scanner: a short line starts a new analysis block (title), labelled
    lines (``背景：…`` / ``**方法**：…``) accumulate parts into the current
    block. Blocks with >= 3 parts are kept. The final article inside the CLI
    response box has no ``**`` around titles — both forms are accepted.
    """
    analyses: list[dict[str, Any]] = []
    if not reports_dir.is_dir():
        return analyses
    NOISE_PREFIXES = ("let me", "actually", "structure:", "note:", "use ",
                      "keep ", "aim ", "total ", "i ", "draft", "the ",
                      "-", ">", "#", "```", "┊", "┌", "└", "╭", "╰", "│",
                      "—", "📚", "本期", "元趋势", "本周", "建议", "数据勘误",
                      "resume", "session", "query", "initializing", "reasoning",
                      "title:", "url", "arxiv", "huggingface", "https",
                      "输出规范", "执行要求", "数据源", "观察结论与")
    part_re = re.compile(
        r"^\*{0,2}(背景|现象|观点|方法(?:效果)?|实验设计|观察结论|局限)\*{0,2}[：:]\s*(.*)$"
    )
    title_re = re.compile(r"^(?:\*{1,2})?\s*\d*\.?\s*(.+?)\s*(?:\*{1,2})?$")

    def flush(title: str, parts: dict[str, str]) -> None:
        if title and len(parts) >= 3:
            analyses.append({"title": title, "parts": parts})

    title = ""
    parts: dict[str, str] = {}
    for raw in (p.read_text(encoding="utf-8", errors="replace")
                for p in sorted(reports_dir.glob("*.md"))):
        for line in raw.splitlines():
            s = line.strip()
            if not s:
                continue
            pm = part_re.match(s)
            if pm:
                key = "方法" if pm.group(1) == "方法效果" else pm.group(1)
                parts[key] = pm.group(2).strip()
                continue
            low = s.lower()
            if low.startswith(NOISE_PREFIXES) or len(s) >= 120:
                continue
            tm = title_re.match(s)
            if tm:
                cand = tm.group(1).strip().strip("**").strip()
                if cand and "：" not in cand.split(":")[0][:6]:
                    flush(title, parts)
                    title = cand
                    parts = {}
    flush(title, parts)
    return analyses


def _title_tokens(title: str) -> set[str]:
    """Distinctive English tokens (lowercased) from a title, for fuzzy
    cross-title matching between report headings and note titles.

    Only proper-name-like tokens count (Title-Case start, or containing
    ``-``/``.``/``#``), length >= 5, minus common filler words. This keeps
    ``AssistantBench``/``UI-TARS``/``STRACE`` while dropping generic words
    like ``deep``/``research``/``agent`` that would cause false matches.
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9+.#-]{2,}", title)
    stop = {"the", "and", "for", "with", "from", "via", "into", "using",
            "based", "toward", "towards", "benchmark", "benchmarks", "bench",
            "agent", "agents", "model", "models", "learning", "paper",
            "arxiv", "deep", "research", "reliable", "system", "systems",
            "framework", "method", "methods", "approach", "approaches"}
    out: set[str] = set()
    for w in words:
        if w.lower() in stop or len(w) < 5:
            continue
        if not (w[0].isupper() or any(c in w for c in "-.#")):
            continue
        out.add(w.lower())
    return out


def _match_analysis(title: str, analyses: list[dict[str, Any]]) -> dict[str, str] | None:
    """Fuzzy match an item title against parsed report analyses.

    Match by full-title containment first, then by shared technical tokens
    (e.g. report title ``1. AssistantBench：…`` vs note title
    ``AssistantBench: Can Web Agents…`` share the token ``AssistantBench``).
    When several blocks match (reasoning draft + final article), return the
    one with the most labelled parts.
    """
    norm = lambda s: re.sub(r"[\s:：,，()（）\"'*#]", "", s.lower())
    target = norm(title)
    target_tokens = _title_tokens(title)
    hits: list[dict[str, str]] = []
    for a in analyses:
        a_title = str(a.get("title") or "")
        a_norm = norm(a_title)
        if target and a_norm and (a_norm in target or target in a_norm):
            hits.append(a["parts"])
            continue
        tokens = target_tokens & _title_tokens(a_title)
        if tokens:
            hits.append(a["parts"])
    if not hits:
        return None
    return max(hits, key=len)


def _backfill_analysis(note_body: str, parts: dict[str, str] | None) -> str:
    if not parts:
        return note_body
    for part in SEVEN_PARTS:
        text = parts.get(part, "")
        note_body = note_body.replace(
            f"**{part}**：\n",
            f"**{part}**：{text}\n" if text else f"**{part}**：\n",
        )
    return note_body


def _index_md(papers: list[dict[str, Any]], news_count: int,
              reports_count: int,
              extra_dirs: dict[str, list[str]] | None = None) -> str:
    lines = [
        "# Research Library Index",
        "",
        f"> 自动生成于 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — 由 export_obsidian.py 维护",
        "",
        f"- 论文笔记：{len(papers)} · 新闻：{news_count} · 周报归档：{reports_count}",
        "",
    ]
    # Curated wiki layers (concepts / comparisons) — maintained by the agent,
    # listed here so the index is a single navigation entry point.
    extra_dirs = extra_dirs or {}
    section_titles = {"concepts": "## 概念", "comparisons": "## 对比"}
    for dirname, files in extra_dirs.items():
        if not files:
            continue
        lines.append(section_titles.get(dirname, f"## {dirname}"))
        lines.append("")
        for f in sorted(files):
            stem = f[:-3] if f.endswith(".md") else f
            lines.append(f"- [[{stem}]]")
        lines.append("")
    lines.append("## 按主题")
    lines.append("")
    by_topic: dict[str, list[str]] = {}
    for p in papers:
        for t in p.get("topics") or ["untagged"]:
            by_topic.setdefault(t, []).append(p["title"])
    for topic in sorted(by_topic):
        lines.append(f"### {topic} ({len(by_topic[topic])})")
        for title in sorted(by_topic[topic]):
            lines.append(f"- [[{title}]]")
        lines.append("")
    lines += [
        "## 按状态",
        "",
        "| 状态 | 数量 |",
        "|------|------|",
    ]
    by_status: dict[str, int] = {}
    for p in papers:
        by_status[p.get("status") or "discovered"] = by_status.get(p.get("status") or "discovered", 0) + 1
    for status in sorted(by_status):
        lines.append(f"| {status} | {by_status[status]} |")
    return "\n".join(lines) + "\n"


def export(vault_path: Path | str, db_path: Path | str,
           reports_dir: Path | str | None = None,
           full: bool = False) -> dict[str, int]:
    """Export library items to the Obsidian vault. Returns export stats."""
    vault = Path(vault_path)
    lib = vault / LIBRARY_SUBDIR
    papers_dir = lib / "01 - Papers"
    news_dir = lib / "02 - News"
    reports_out_dir = lib / "03 - Weekly Reports"
    templates_dir = lib / "99 - Templates"
    for d in (papers_dir, news_dir, reports_out_dir, templates_dir):
        d.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    reports = _parse_report_analyses(Path(reports_dir)) if reports_dir else []
    analyses = reports

    stats = {"papers": 0, "news": 0, "updated": 0, "created": 0}
    papers_meta: list[dict[str, Any]] = []

    for row in conn.execute(
        "SELECT * FROM research_items ORDER BY first_discovered_at"
    ):
        item = dict(row)
        item_id = item["id"]
        topics = _read_topics(conn, item_id)
        sources = _read_sources(conn, item_id)
        rec = _read_recommendation(conn, item_id)
        arxiv_id = _extract_arxiv_id(item)
        title = item.get("title") or arxiv_id or item_id
        date = _date(item.get("published_at") or item.get("first_discovered_at"))
        prefix = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fname = _note_filename(item)
        is_news = item.get("item_type") in ("research_news", "business_news",
                                            "product_release", "opinion")
        dest_dir = news_dir if is_news else papers_dir
        path = dest_dir / fname

        fields = {
            "type": "news" if is_news else "paper",
            "title": title,
            "arxiv_id": arxiv_id,
            "url": item.get("url") or "",
            "authors": json.loads(item.get("authors_json") or "[]"),
            "published": _date(item.get("published_at")),
            "discovered": _date(item.get("first_discovered_at")),
            "topics": topics,
            "status": item.get("status") or "discovered",
            "source": sources[0] if sources else "",
            "score": rec["score"] if rec else None,
            "recommended": rec["recommended_at"] if rec else None,
            "tags": ["news" if is_news else "paper"] + topics,
        }

        body = [f"# {title}", ""]
        body.append(_summary_block(item))
        body.append(_analysis_section())
        if rec and rec.get("rationale"):
            body.append(f"**推荐理由**：{rec['rationale']}")
            body.append("")
        if item.get("url"):
            body.append(f"**原文**：{item['url']}")
        body.append("")
        body.append("## 相关笔记")
        body.append("")
        body.append("<!-- 手动添加 [[wikilinks]] -->")
        body.append("")

        note_body = "\n".join(body)
        parts = _match_analysis(title, analyses)
        note_body = _backfill_analysis(note_body, parts)

        content = _frontmatter(fields) + "\n" + note_body
        existed = path.exists()
        if existed and path.read_text(encoding="utf-8") == content:
            pass  # unchanged
        elif existed and _has_analysis(path):
            # Agent-written analysis notes (seven-part 解读 filled in by the
            # deep-research agent) are authoritative — do not overwrite them
            # with the empty scaffold.
            stats["agent_kept"] = stats.get("agent_kept", 0) + 1
        else:
            path.write_text(content, encoding="utf-8")
            stats["updated" if existed else "created"] += 1

        if not is_news:
            papers_meta.append({"title": title, "topics": topics,
                                "status": fields["status"]})
            stats["papers"] += 1
        else:
            stats["news"] += 1

    # Weekly reports archive copy (deep-research outputs, cleaned).
    reports_count = 0
    if reports_dir:
        src = Path(reports_dir)
        for rpath in sorted(src.glob("weekly-*.md")):
            text = rpath.read_text(encoding="utf-8", errors="replace")
            out = reports_out_dir / rpath.name
            if not out.exists() or out.read_text(encoding="utf-8") != text:
                out.write_text(text, encoding="utf-8")
                reports_count += 1

    # Templates.
    (templates_dir / "paper-template.md").write_text(
        "---\ntype: paper\nstatus: unread\ntags: [paper]\n---\n\n"
        "# {{title}}\n\n> summary\n\n## 解读\n\n"
        + "\n".join(f"**{p}**：" for p in SEVEN_PARTS) + "\n",
        encoding="utf-8",
    )

    # Index (MOC) — includes curated wiki layers (concepts/comparisons).
    extra_dirs: dict[str, list[str]] = {}
    for d in ("concepts", "comparisons"):
        dd = lib / d
        if dd.is_dir():
            extra_dirs[d] = sorted(
                f.name for f in dd.glob("*.md") if f.name != "SCHEMA.md"
            )
    index = _index_md(papers_meta, stats["news"], reports_count, extra_dirs)
    index_path = lib / "00 - Index.md"
    index_path.write_text(index, encoding="utf-8")

    conn.close()
    stats["reports_archived"] = reports_count
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Research Copilot library to Obsidian vault")
    parser.add_argument("--vault", default=str(DEFAULT_VAULT), help="Obsidian vault path")
    parser.add_argument("--db", default=str(Path.home() / ".hermes/research-copilot/library.db"))
    parser.add_argument("--reports", default=str(Path.home() / ".hermes/research-copilot/reports"))
    parser.add_argument("--full", action="store_true", help="force full rewrite")
    args = parser.parse_args(argv)
    stats = export(args.vault, args.db, args.reports, full=args.full)
    print(f"exported: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
