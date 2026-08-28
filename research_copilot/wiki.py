"""Deterministic health checks for the Research Copilot LLM Wiki.

This module deliberately has no model dependency.  Export and lint are CLI
operations at the edge of Hermes, while SQLite remains the profile-scoped
source of truth for collected research evidence.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


WIKILINK_RE = re.compile(r"\[\[(.+?)\]\]")
ANALYSIS_PARTS = ("背景", "现象", "观点", "方法", "实验设计", "观察结论", "局限")
CONTENT_DIRS = ("01 - Papers", "02 - News", "03 - Weekly Reports", "concepts", "comparisons", "queries")


@dataclass(frozen=True)
class WikiIssue:
    severity: str
    code: str
    path: str
    message: str


@dataclass
class WikiAudit:
    root: str
    markdown_files: int = 0
    papers: int = 0
    news: int = 0
    reports: int = 0
    curated_pages: int = 0
    links: int = 0
    issues: list[WikiIssue] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def warnings(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["errors"] = self.errors
        value["warnings"] = self.warnings
        return value


@dataclass(frozen=True)
class WikiReconcileReport:
    scanned: int
    analyzed_pages: int
    matched_items: int
    candidates: tuple[str, ...]
    updated: tuple[str, ...]
    unmatched: tuple[str, ...]


def resolve_wiki_root(vault_override: str | Path | None = None) -> tuple[Path, str, Path]:
    from .runtime import wiki_config

    config = wiki_config()
    vault = Path(vault_override).expanduser() if vault_override else config["vault_path"]
    return vault / config["library_subdir"], config["library_subdir"], vault


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---\n") or "\n---\n" not in text:
        return {}
    raw = text.split("\n---\n", 1)[0][4:]
    try:
        value = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def _targets(text: str) -> list[str]:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    targets: list[str] = []
    for raw in WIKILINK_RE.findall(text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            targets.append(target)
    return targets


def _analysis_count(text: str) -> int:
    labels = "|".join(re.escape(value) for value in (*ANALYSIS_PARTS, "原文"))
    count = 0
    for part in ANALYSIS_PARTS:
        match = re.search(
            rf"\*\*{re.escape(part)}\*\*：(?P<value>.*?)"
            rf"(?=\n\s*\*\*(?:{labels})\*\*：|\n## |\Z)",
            text,
            flags=re.DOTALL,
        )
        count += bool(match and match.group("value").strip())
    return count


def audit_wiki(root: str | Path) -> WikiAudit:
    root = Path(root)
    audit = WikiAudit(root=str(root))
    if not root.is_dir():
        audit.issues.append(WikiIssue("error", "wiki_missing", ".", f"Wiki directory does not exist: {root}"))
        return audit

    files = sorted(root.rglob("*.md"))
    audit.markdown_files = len(files)
    texts = {path: path.read_text(encoding="utf-8", errors="replace") for path in files}
    metadata = {path: _frontmatter(text) for path, text in texts.items()}
    audit.papers = sum(path.parent.name == "01 - Papers" for path in files)
    audit.news = sum(path.parent.name == "02 - News" for path in files)
    audit.reports = sum(path.parent.name == "03 - Weekly Reports" for path in files)
    curated = [path for path in files if path.parent.name in {"concepts", "comparisons", "queries"}]
    audit.curated_pages = len(curated)

    names: dict[str, Path] = {}
    aliases: dict[str, Path] = {}
    for path in files:
        names[path.stem.casefold()] = path
        raw_aliases = metadata[path].get("aliases") or []
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        if isinstance(raw_aliases, list):
            for alias in raw_aliases:
                aliases[str(alias).casefold()] = path

    inbound: Counter[Path] = Counter()
    for source, text in texts.items():
        if source.name != "00 - Index.md" and source.parent.name not in CONTENT_DIRS:
            continue
        for target in _targets(text):
            audit.links += 1
            key = Path(target).name.casefold()
            destination = names.get(key) or aliases.get(key)
            if destination is None:
                audit.issues.append(WikiIssue(
                    "error", "broken_link", str(source.relative_to(root)), f"Broken wikilink: [[{target}]]",
                ))
            else:
                inbound[destination] += 1

    index_path = root / "00 - Index.md"
    index_text = texts.get(index_path, "")
    index_targets = {Path(target).name.casefold() for target in _targets(index_text)}
    indexed_paths = {names.get(key) or aliases.get(key) for key in index_targets}
    for path in files:
        if path.parent.name not in CONTENT_DIRS:
            continue
        if path not in indexed_paths:
            audit.issues.append(WikiIssue(
                "error", "missing_from_index", str(path.relative_to(root)), "Content page is not listed in 00 - Index.md",
            ))

    declared = re.search(r"论文笔记：(\d+)\s*·\s*新闻：(\d+)\s*·\s*周报归档：(\d+)", index_text)
    if declared and tuple(map(int, declared.groups())) != (audit.papers, audit.news, audit.reports):
        audit.issues.append(WikiIssue(
            "error", "index_count_mismatch", "00 - Index.md",
            f"Declared counts {declared.groups()} do not match filesystem "
            f"({audit.papers}, {audit.news}, {audit.reports})",
        ))

    schema_text = texts.get(root / "SCHEMA.md", "")
    taxonomy = ""
    if "## Tag Taxonomy" in schema_text:
        taxonomy = schema_text.split("## Tag Taxonomy", 1)[1].split("\n## ", 1)[0]
    allowed_tags = set(re.findall(r"`([^`]+)`", taxonomy))
    if allowed_tags:
        for path, fields in metadata.items():
            raw_tags = fields.get("tags") or []
            if isinstance(raw_tags, str):
                raw_tags = [raw_tags]
            if not isinstance(raw_tags, list):
                continue
            unknown = sorted(str(tag) for tag in raw_tags if str(tag) not in allowed_tags)
            if unknown:
                audit.issues.append(WikiIssue(
                    "warning", "unknown_tag", str(path.relative_to(root)),
                    "Tags missing from SCHEMA.md taxonomy: " + ", ".join(unknown),
                ))

    for path in curated:
        fields = metadata[path]
        required = ("type", "title", "created", "updated", "tags", "sources")
        missing = [key for key in required if not fields.get(key)]
        if missing:
            audit.issues.append(WikiIssue(
                "error", "frontmatter_missing", str(path.relative_to(root)),
                "Missing required frontmatter: " + ", ".join(missing),
            ))
        if inbound[path] == 0:
            audit.issues.append(WikiIssue("warning", "orphan", str(path.relative_to(root)), "Curated page has no inbound wikilinks"))
        if len(_targets(texts[path])) < 2:
            audit.issues.append(WikiIssue("warning", "too_few_links", str(path.relative_to(root)), "Curated page has fewer than two outbound wikilinks"))
        if fields.get("contested") is True:
            audit.issues.append(WikiIssue("warning", "contested", str(path.relative_to(root)), "Page has unresolved contradictions"))
        if str(fields.get("confidence") or "").casefold() == "low":
            audit.issues.append(WikiIssue("warning", "low_confidence", str(path.relative_to(root)), "Page is marked low confidence"))

    identity: dict[tuple[str, str], list[Path]] = defaultdict(list)
    for path in files:
        if path.parent.name != "01 - Papers":
            continue
        fields = metadata[path]
        arxiv_id = str(fields.get("arxiv_id") or "").strip()
        title = str(fields.get("title") or "").strip().casefold()
        key = ("arxiv", arxiv_id) if arxiv_id else ("title", title)
        if key[1]:
            identity[key].append(path)
        links = _targets(texts[path])
        title = str(fields.get("title") or "").strip()
        raw_aliases = fields.get("aliases") or []
        if isinstance(raw_aliases, str):
            raw_aliases = [raw_aliases]
        if title and title.casefold() != path.stem.casefold() and title not in raw_aliases:
            audit.issues.append(WikiIssue(
                "error", "title_alias_missing", str(path.relative_to(root)),
                "Filename differs from title but aliases does not contain the title",
            ))
        if not links:
            audit.issues.append(WikiIssue("warning", "paper_unlinked", str(path.relative_to(root)), "Paper has no outbound wikilinks"))
        if _analysis_count(texts[path]) == 0:
            audit.issues.append(WikiIssue("warning", "analysis_empty", str(path.relative_to(root)), "Seven-part analysis is empty"))
    for (kind, value), paths in identity.items():
        if len(paths) > 1:
            audit.issues.append(WikiIssue(
                "error", "duplicate_paper", ", ".join(str(path.relative_to(root)) for path in paths),
                f"Duplicate paper {kind}: {value}",
            ))
    for path, text in texts.items():
        if len(text.splitlines()) > 200 and path.parent.name in {"concepts", "comparisons", "queries"}:
            audit.issues.append(WikiIssue("warning", "page_too_large", str(path.relative_to(root)), "Curated page exceeds 200 lines"))
    log_text = texts.get(root / "log.md", "")
    if len(re.findall(r"^## \[", log_text, flags=re.MULTILINE)) > 500:
        audit.issues.append(WikiIssue("warning", "log_rotation", "log.md", "Log has more than 500 entries and should be rotated"))
    return audit


def reconcile_wiki_analysis(
    root: str | Path,
    repository: Any,
    *,
    apply: bool,
    updated_at: Any,
    minimum_parts: int = 3,
) -> WikiReconcileReport:
    """Promote substantial Wiki analyses without inferring reader progress."""
    root = Path(root)
    papers = sorted((root / "01 - Papers").glob("*.md"))
    rows = repository.connection.execute(
        "SELECT id,canonical_key,title,agent_analysis_status FROM research_items"
    ).fetchall()
    by_key = {str(row["canonical_key"]): row for row in rows}
    by_title: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        by_title[str(row["title"]).strip().casefold()].append(row)

    candidates: list[str] = []
    updated: list[str] = []
    unmatched: list[str] = []
    analyzed_pages = 0
    matched: set[str] = set()
    for path in papers:
        text = path.read_text(encoding="utf-8", errors="replace")
        if _analysis_count(text) < minimum_parts:
            continue
        analyzed_pages += 1
        fields = _frontmatter(text)
        canonical_key = str(fields.get("canonical_key") or "").strip()
        arxiv_id = str(fields.get("arxiv_id") or "").strip()
        title = str(fields.get("title") or "").strip().casefold()
        row = by_key.get(canonical_key)
        if row is None and arxiv_id:
            row = by_key.get(f"arxiv:{arxiv_id}")
        if row is None and len(by_title.get(title, ())) == 1:
            row = by_title[title][0]
        if row is None:
            unmatched.append(str(path.relative_to(root)))
            continue
        item_id = str(row["id"])
        matched.add(item_id)
        if str(row["agent_analysis_status"]) in {"deep_researched", "synthesized"}:
            continue
        if item_id not in candidates:
            candidates.append(item_id)
            if apply:
                repository.set_analysis_status(
                    item_id, status="deep_researched", updated_at=updated_at,
                )
                updated.append(item_id)
    return WikiReconcileReport(
        scanned=len(papers), analyzed_pages=analyzed_pages,
        matched_items=len(matched), candidates=tuple(candidates),
        updated=tuple(updated), unmatched=tuple(unmatched),
    )


def render_audit(audit: WikiAudit, *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(audit.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    lines = [
        f"Research Wiki health — {audit.root}",
        f"pages={audit.markdown_files} papers={audit.papers} news={audit.news} reports={audit.reports} curated={audit.curated_pages}",
        f"links={audit.links} errors={audit.errors} warnings={audit.warnings}",
    ]
    grouped: dict[str, list[WikiIssue]] = defaultdict(list)
    for issue in audit.issues:
        grouped[issue.severity].append(issue)
    for severity in ("error", "warning"):
        if not grouped[severity]:
            continue
        lines.append("")
        lines.append(severity.upper())
        for issue in grouped[severity]:
            lines.append(f"- [{issue.code}] {issue.path}: {issue.message}")
    return "\n".join(lines)
