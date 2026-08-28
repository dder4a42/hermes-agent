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
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# Old topic ids seen in historical item_topics rows -> current active ids.
TOPIC_ID_MAP = {
    "research-agent": "search-agent",
    "multimodal-lmm": "mllm",
    "evaluation-harness": "search-agent",
    "agent-infra": "long-horizon-agent",
    "inference": "long-horizon-agent",
}

SEVEN_PARTS = ["背景", "现象", "观点", "方法", "实验设计", "观察结论", "局限"]

STRUCTURED_ANALYSIS_FIELDS = {
    "背景": "background",
    "现象": "phenomenon",
    "观点": "thesis",
    "方法": "method",
    "实验设计": "experiment_design",
    "观察结论": "findings",
    "局限": "limitations",
}

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
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(dt)
        if parsed is not None:
            return parsed.strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        pass
    return ""


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


def _read_structured_analysis(
    conn: sqlite3.Connection, item_ids: list[str],
) -> dict[str, str] | None:
    rows = []
    for item_id in item_ids:
        row = conn.execute(
            """SELECT artifact_json FROM deep_research_artifacts
               WHERE item_id=? ORDER BY generated_at DESC,id DESC LIMIT 1""",
            (item_id,),
        ).fetchone()
        if row is not None:
            rows.append(row)
    if not rows:
        return None
    artifact = json.loads(rows[0]["artifact_json"])
    analysis = artifact.get("analysis") or {}
    if not isinstance(analysis, dict):
        return None
    return {
        label: str(analysis.get(field) or "").strip()
        for label, field in STRUCTURED_ANALYSIS_FIELDS.items()
    }


def _frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    preferred = ("type", "title", "aliases", "canonical_key", "canonical_keys", "arxiv_id", "url",
                 "authors", "published", "discovered", "topics", "status",
                 "agent_analysis_status", "user_learning_status",
                 "source", "sources", "score", "recommended", "tags")
    ordered = list(preferred) + sorted(key for key in fields if key not in preferred)
    for key in ordered:
        value = fields.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            rendered = "[" + ", ".join(json.dumps(str(v), ensure_ascii=False) for v in value) + "]"
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = json.dumps(str(value), ensure_ascii=False)
        lines.append(f"{key}: {rendered}")
    lines.append("---")
    return "\n".join(lines)


def _split_note(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n") or "\n---\n" not in text:
        return {}, text
    raw, body = text[4:].split("\n---\n", 1)
    try:
        fields = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        fields = {}
    return (fields if isinstance(fields, dict) else {}), body


def _body_section(body: str, heading: str, next_heading: str | None = None) -> str:
    marker = f"## {heading}"
    start = body.find(marker)
    if start < 0:
        return ""
    if next_heading:
        end = body.find(f"## {next_heading}", start + len(marker))
        if end >= 0:
            return body[start:end].rstrip() + "\n\n"
    return body[start:].rstrip() + "\n"


def _has_real_wikilink(text: str) -> bool:
    without_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return bool(re.search(r"\[\[[^\]]+\]\]", without_comments))


def _merge_curated_note(path: Path, fields: dict[str, Any], generated_body: str) -> str:
    """Refresh generated fields while preserving curator-owned sections.

    ``# title`` plus the generated summary/recommendation/source block are
    rebuilt from SQLite.  ``## 解读`` and ``## 相关笔记`` are selected from the
    existing note when they contain Agent work.  This avoids the old all-or-
    nothing behavior where protecting one analysis line froze stale metadata.
    """
    if not path.exists():
        return _frontmatter(fields) + "\n" + generated_body
    existing = path.read_text(encoding="utf-8", errors="replace")
    old_fields, old_body = _split_note(existing)
    merged_fields = {**old_fields, **fields}
    generated_analysis = _body_section(generated_body, "解读", "相关笔记")
    generated_related = _body_section(generated_body, "相关笔记")
    old_analysis = _body_section(old_body, "解读", "相关笔记")
    old_related = _body_section(old_body, "相关笔记")
    prefix = generated_body.split("## 解读", 1)[0].rstrip() + "\n\n"
    analysis = old_analysis if _has_analysis(path) else generated_analysis
    related = old_related if _has_real_wikilink(old_related) else generated_related
    return _frontmatter(merged_fields) + "\n" + prefix + analysis + related


def _merge_stale_curated_sections(target: Path, stale: Path) -> bool:
    """Copy curator-owned work from an obsolete generated path into its target.

    Old exporter versions truncated slugs and sometimes emitted malformed date
    prefixes.  Those files are safe to retire only after preserving the two
    sections explicitly owned by the curator contract.
    """
    target_text = target.read_text(encoding="utf-8", errors="replace")
    stale_text = stale.read_text(encoding="utf-8", errors="replace")
    target_fields, target_body = _split_note(target_text)
    stale_fields, stale_body = _split_note(stale_text)
    target_analysis = _body_section(target_body, "解读", "相关笔记")
    stale_analysis = _body_section(stale_body, "解读", "相关笔记")
    target_related = _body_section(target_body, "相关笔记")
    stale_related = _body_section(stale_body, "相关笔记")
    analysis = target_analysis
    if not any(_analysis_part_value(target_analysis, part) for part in SEVEN_PARTS):
        if any(_analysis_part_value(stale_analysis, part) for part in SEVEN_PARTS):
            analysis = stale_analysis
    related = target_related
    if not _has_real_wikilink(target_related) and _has_real_wikilink(stale_related):
        related = stale_related
    aliases = target_fields.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    stale_title = str(stale_fields.get("title") or "").strip()
    if stale_title and stale_title not in aliases:
        target_fields["aliases"] = [*aliases, stale_title]
    prefix = target_body.split("## 解读", 1)[0].rstrip() + "\n\n"
    merged = _frontmatter(target_fields) + "\n" + prefix + analysis + related
    changed = merged != target_text
    if changed:
        target.write_text(merged, encoding="utf-8")
    return changed


def _has_analysis(path: Path) -> bool:
    """True if a note already carries filled-in seven-part analysis (written
    by the deep-research agent), so the exporter should not overwrite it."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(_analysis_part_value(text, part) for part in SEVEN_PARTS)


def _analysis_part_value(text: str, part: str) -> str:
    """Return one analysis value in either inline or next-line form."""
    labels = "|".join(
        re.escape(value) for value in (*SEVEN_PARTS, "推荐理由", "原文")
    )
    match = re.search(
        rf"\*\*{re.escape(part)}\*\*：(?P<value>.*?)"
        rf"(?=\n\s*\*\*(?:{labels})\*\*：|\n## |\Z)",
        text,
        flags=re.DOTALL,
    )
    return match.group("value").strip() if match else ""


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


def _published_report_text(text: str) -> str:
    """Extract the final article from a Hermes CLI transcript.

    Reports created by older cron jobs captured stdout wholesale.  Keeping
    their prompt, reasoning panels, tool activity and session identifiers in
    the knowledge vault violates the raw/derived boundary, so the exporter
    applies the same deterministic final-response boundary used for delivery.
    """
    text = re.sub(r"^Query:.*?Initializing agent", "", text, flags=re.DOTALL)
    positions = [
        match.start()
        for match in re.finditer(r"📚 深度科研周报 · \d{4}-\d{2}-\d{2}", text)
    ]
    if positions:
        text = text[positions[-1]:]
        text = re.split(r"Resume this session|Session:", text, maxsplit=1)[0]
    text = re.sub(r"^[╭╰╮╯┌└┐┘─│+]+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^  ┊ .*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^Initializing agent.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*⚠ tirith.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*Reasoning\s*$", "", text, flags=re.MULTILINE)
    return text.strip() + "\n" if text.strip() else ""


def _wiki_link(page: dict[str, Any]) -> str:
    stem = str(page["stem"])
    title = str(page.get("title") or stem)
    return f"[[{stem}]]" if title == stem else f"[[{stem}|{title}]]"


def _index_md(papers: list[dict[str, Any]], news: list[dict[str, Any]],
              reports: list[dict[str, Any]],
              extra_dirs: dict[str, list[str]] | None = None) -> str:
    lines = [
        "# Research Library Index",
        "",
        f"> 自动生成于 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — 由 export_obsidian.py 维护",
        "",
        f"- 论文笔记：{len(papers)} · 新闻：{len(news)} · 周报归档：{len(reports)}",
        "",
    ]
    # Curated wiki layers (concepts / comparisons) — maintained by the agent,
    # listed here so the index is a single navigation entry point.
    extra_dirs = extra_dirs or {}
    section_titles = {"concepts": "## 概念", "comparisons": "## 对比", "queries": "## 查询与综合"}
    for dirname, files in extra_dirs.items():
        if not files:
            continue
        lines.append(section_titles.get(dirname, f"## {dirname}"))
        lines.append("")
        for f in sorted(files):
            stem = f[:-3] if f.endswith(".md") else f
            lines.append(f"- [[{stem}]]")
        lines.append("")
    if news:
        lines.extend(["## 新闻", ""])
        lines.extend(f"- {_wiki_link(page)}" for page in sorted(news, key=lambda page: str(page["title"])))
        lines.append("")
    if reports:
        lines.extend(["## 周报", ""])
        lines.extend(f"- {_wiki_link(page)}" for page in sorted(reports, key=lambda page: str(page["title"])))
        lines.append("")
    lines.append("## 按主题")
    lines.append("")
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for p in papers:
        for t in p.get("topics") or ["untagged"]:
            by_topic.setdefault(t, []).append(p)
    for topic in sorted(by_topic):
        lines.append(f"### {topic} ({len(by_topic[topic])})")
        for page in sorted(by_topic[topic], key=lambda page: str(page["title"])):
            lines.append(f"- {_wiki_link(page)}")
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
           full: bool = False, library_subdir: str = LIBRARY_SUBDIR) -> dict[str, int]:
    """Export library items to the Obsidian vault. Returns export stats."""
    vault = Path(vault_path)
    lib = vault / library_subdir
    papers_dir = lib / "01 - Papers"
    news_dir = lib / "02 - News"
    reports_out_dir = lib / "03 - Weekly Reports"
    templates_dir = lib / "99 - Templates"
    concepts_dir = lib / "concepts"
    comparisons_dir = lib / "comparisons"
    queries_dir = lib / "queries"
    for d in (papers_dir, news_dir, reports_out_dir, templates_dir,
              concepts_dir, comparisons_dir, queries_dir):
        d.mkdir(parents=True, exist_ok=True)

    schema_path = lib / "SCHEMA.md"
    if not schema_path.exists():
        schema_path.write_text(
            "# Wiki Schema — Research Library\n\n"
            "> SQLite is the authoritative evidence layer; this directory is the compiled, curated wiki.\n\n"
            "## Ownership\n\n"
            "- `01 - Papers/` and `02 - News/`: generated metadata and summaries; Agent owns `## 解读` and `## 相关笔记`.\n"
            "- `concepts/`, `comparisons/`, `queries/`: Agent-curated synthesis pages.\n"
            "- `00 - Index.md`: generated navigation; never edit manually.\n"
            "- `log.md`: append-only curation and export history.\n\n"
            "## Curated page contract\n\n"
            "Required frontmatter: `type`, `title`, `created`, `updated`, `tags`, `sources`.\n"
            "Every curated page needs at least two outbound `[[wikilinks]]`. Conflicts are recorded, not overwritten.\n",
            encoding="utf-8",
        )
    log_path = lib / "log.md"
    if not log_path.exists():
        log_path.write_text(
            "# Wiki Log\n\n> Append-only Research Wiki activity.\n\n",
            encoding="utf-8",
        )

    database = Path(db_path).resolve()
    conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    reports = _parse_report_analyses(Path(reports_dir)) if reports_dir else []
    analyses = reports

    stats = {
        "papers": 0, "news": 0, "updated": 0, "created": 0,
        "merged_records": 0, "retired_duplicates": 0,
    }
    papers_meta: list[dict[str, Any]] = []
    exported_paths: set[Path] = set()
    exported_identities: dict[str, Path] = {}

    raw_items = [dict(row) for row in conn.execute(
        "SELECT * FROM research_items ORDER BY first_discovered_at"
    )]
    grouped_items: dict[tuple[bool, str, str], list[dict[str, Any]]] = {}
    for candidate in raw_items:
        candidate_is_news = candidate.get("item_type") in (
            "research_news", "business_news", "product_release", "opinion",
        )
        grouped_items.setdefault((
            candidate_is_news,
            _note_filename(candidate),
            str(candidate.get("title") or "").strip().casefold(),
        ), []).append(candidate)

    items: list[dict[str, Any]] = []
    for group in grouped_items.values():
        # Prefer canonical paper metadata over a project/home-page discovery,
        # while retaining every backing Library identity for provenance.
        primary = max(
            group,
            key=lambda value: (
                str(value.get("canonical_key") or "").startswith("arxiv:"),
                bool(value.get("summary")),
                bool(value.get("authors_json") and value.get("authors_json") != "[]"),
            ),
        ).copy()
        primary["_merged_item_ids"] = [str(value["id"]) for value in group]
        primary["_canonical_keys"] = (
            [str(value.get("canonical_key") or "") for value in group if value.get("canonical_key")]
            if len(group) > 1 else []
        )
        stats["merged_records"] += len(group) - 1
        items.append(primary)
    items.sort(key=lambda value: str(value.get("first_discovered_at") or ""))
    filename_counts = Counter(_note_filename(item) for item in items)
    for item in items:
        filename = _note_filename(item)
        if filename_counts[filename] > 1:
            identity = str(item.get("canonical_key") or item.get("id") or item.get("title") or "")
            suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
            item["_export_filename"] = f"{Path(filename).stem}-{suffix}.md"
        else:
            item["_export_filename"] = filename

    for item in items:
        item_id = item["id"]
        backing_ids = item.get("_merged_item_ids") or [item_id]
        topics = list(dict.fromkeys(
            topic for backing_id in backing_ids for topic in _read_topics(conn, str(backing_id))
        ))
        sources = list(dict.fromkeys(
            source for backing_id in backing_ids for source in _read_sources(conn, str(backing_id))
        ))
        recommendations = [
            recommendation for backing_id in backing_ids
            if (recommendation := _read_recommendation(conn, str(backing_id))) is not None
        ]
        rec = max(recommendations, key=lambda value: float(value.get("score") or 0)) if recommendations else None
        arxiv_id = _extract_arxiv_id(item)
        title = item.get("title") or arxiv_id or item_id
        date = _date(item.get("published_at") or item.get("first_discovered_at"))
        prefix = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fname = str(item["_export_filename"])
        is_news = item.get("item_type") in ("research_news", "business_news",
                                            "product_release", "opinion")
        dest_dir = news_dir if is_news else papers_dir
        path = dest_dir / fname
        exported_paths.add(path)
        canonical_identity = str(item.get("canonical_key") or "").strip()
        url_identity = str(item.get("url") or "").strip()
        if canonical_identity:
            exported_identities[canonical_identity] = path
        if url_identity:
            exported_identities[f"url:{url_identity}"] = path

        fields = {
            "type": "news" if is_news else "paper",
            "title": title,
            "aliases": [title],
            "canonical_key": item.get("canonical_key") or "",
            "canonical_keys": item.get("_canonical_keys") or [],
            "arxiv_id": arxiv_id,
            "url": item.get("url") or "",
            "authors": json.loads(item.get("authors_json") or "[]"),
            "published": _date(item.get("published_at")),
            "discovered": _date(item.get("first_discovered_at")),
            "topics": topics,
            "status": item.get("workflow_state") or item.get("status") or "discovered",
            "agent_analysis_status": item.get("agent_analysis_status") or "none",
            "user_learning_status": item.get("user_learning_status") or "unseen",
            "source": sources[0] if sources else "",
            "sources": sources,
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
        parts = _read_structured_analysis(conn, [str(value) for value in backing_ids])
        if not parts:
            parts = _match_analysis(title, analyses)
        note_body = _backfill_analysis(note_body, parts)
        note_body = re.sub(r"\n+## 相关笔记", "\n\n## 相关笔记", note_body)

        content = _merge_curated_note(path, fields, note_body)
        content = re.sub(r"\n+## 相关笔记", "\n\n## 相关笔记", content)
        existed = path.exists()
        if existed and not full and path.read_text(encoding="utf-8") == content:
            pass  # unchanged
        else:
            path.write_text(content, encoding="utf-8")
            stats["updated" if existed else "created"] += 1

        if not is_news:
            papers_meta.append({"title": title, "topics": topics,
                                "status": fields["status"], "stem": path.stem})
            stats["papers"] += 1
        else:
            stats["news"] += 1

    # Weekly reports archive copy (deep-research outputs, cleaned).
    reports_copied = 0
    if reports_dir:
        src = Path(reports_dir)
        published = src / "published"
        report_sources = published if published.is_dir() else src
        for rpath in sorted(report_sources.glob("weekly-*.md")):
            text = _published_report_text(
                rpath.read_text(encoding="utf-8", errors="replace")
            )
            if not text:
                continue
            out = reports_out_dir / rpath.name
            if not out.exists() or out.read_text(encoding="utf-8") != text:
                out.write_text(text, encoding="utf-8")
                reports_copied += 1

    # Templates.
    (templates_dir / "paper-template.md").write_text(
        "---\ntype: paper\nstatus: discovered\n"
        "agent_analysis_status: none\nuser_learning_status: unseen\n"
        "tags: [paper]\n---\n\n"
        "# {{title}}\n\n> summary\n\n## 解读\n\n"
        + "\n".join(f"**{p}**：" for p in SEVEN_PARTS) + "\n",
        encoding="utf-8",
    )

    # Build the MOC from the final filesystem, not just the current database
    # iteration.  This keeps manually researched pages visible and makes the
    # index an honest view of the vault.
    for existing_path in sorted((*papers_dir.glob("*.md"), *news_dir.glob("*.md"))):
        if existing_path in exported_paths:
            continue
        existing_text = existing_path.read_text(encoding="utf-8", errors="replace")
        existing_fields, existing_body = _split_note(existing_text)
        existing_identity = str(existing_fields.get("canonical_key") or "").strip()
        existing_url = str(existing_fields.get("url") or "").strip()
        target_path = (
            exported_identities.get(existing_identity)
            if existing_identity else None
        ) or (exported_identities.get(f"url:{existing_url}") if existing_url else None)
        if target_path is not None and target_path != existing_path:
            if _merge_stale_curated_sections(target_path, existing_path):
                stats["updated"] += 1
            existing_path.unlink()
            stats["retired_duplicates"] += 1
            continue
        existing_title = str(existing_fields.get("title") or "").strip()
        aliases = existing_fields.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        if existing_title and existing_title not in aliases:
            existing_fields["aliases"] = [*aliases, existing_title]
            normalized = _frontmatter(existing_fields) + "\n" + existing_body
            existing_path.write_text(normalized, encoding="utf-8")
            stats["updated"] += 1

    def page_meta(path: Path, *, default_type: str) -> dict[str, Any]:
        fields, _body = _split_note(path.read_text(encoding="utf-8", errors="replace"))
        topics = fields.get("topics") or []
        if isinstance(topics, str):
            topics = [topics]
        return {
            "title": str(fields.get("title") or path.stem),
            "topics": [str(topic) for topic in topics],
            "status": str(fields.get("status") or "discovered"),
            "type": str(fields.get("type") or default_type),
            "stem": path.stem,
        }

    papers_meta = [page_meta(path, default_type="paper") for path in sorted(papers_dir.glob("*.md"))]
    news_meta = [page_meta(path, default_type="news") for path in sorted(news_dir.glob("*.md"))]
    reports_meta = [
        {"title": path.stem, "topics": [], "status": "archived", "type": "report", "stem": path.stem}
        for path in sorted(reports_out_dir.glob("*.md"))
    ]

    # Index (MOC) — includes curated wiki layers (concepts/comparisons).
    extra_dirs: dict[str, list[str]] = {}
    for d in ("concepts", "comparisons", "queries"):
        dd = lib / d
        if dd.is_dir():
            extra_dirs[d] = sorted(
                f.name for f in dd.glob("*.md") if f.name != "SCHEMA.md"
            )
    index = _index_md(papers_meta, news_meta, reports_meta, extra_dirs)
    index_path = lib / "00 - Index.md"
    index_path.write_text(index, encoding="utf-8")

    if stats["created"] or stats["updated"] or reports_copied:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(
                f"\n## [{timestamp}] export | Research Library\n"
                f"- created: {stats['created']}\n- updated: {stats['updated']}\n"
                f"- retired duplicates: {stats['retired_duplicates']}\n"
                f"- reports copied: {reports_copied}\n\n"
            )
    # Keep the append-only log friendly to ``git diff --check``.  Entries are
    # separated when appending, but the file itself ends with one newline.
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    normalized_log = log_text.rstrip() + "\n"
    if normalized_log != log_text:
        log_path.write_text(normalized_log, encoding="utf-8")

    conn.close()
    stats["papers"] = len(papers_meta)
    stats["news"] = len(news_meta)
    stats["reports_archived"] = reports_copied
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Research Copilot library to Obsidian vault")
    from .runtime import runtime_paths, wiki_config

    runtime = runtime_paths()
    configured = wiki_config()
    parser.add_argument("--vault", default=str(configured["vault_path"]), help="Obsidian vault path")
    parser.add_argument("--db", default=str(runtime["database"]))
    parser.add_argument("--reports", default=str(runtime["reports"]))
    parser.add_argument("--full", action="store_true", help="force full rewrite")
    args = parser.parse_args(argv)
    stats = export(
        args.vault, args.db, args.reports, full=args.full,
        library_subdir=configured["library_subdir"],
    )
    print(f"exported: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
