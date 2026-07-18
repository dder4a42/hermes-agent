"""Deterministic daily Research Library report grouped by evidence shape."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
import json


@dataclass(frozen=True)
class ReportItem:
    id: str
    title: str
    summary: str
    url: str
    item_type: str
    discovered_at: str
    topics: tuple[str, ...] = ()
    evidence_level: str = ""
    publisher_domain: str = ""


@dataclass(frozen=True)
class DailyReport:
    generated_at: datetime
    since: datetime
    papers: tuple[ReportItem, ...]
    news: tuple[ReportItem, ...]
    opinions: tuple[ReportItem, ...]
    engineering: tuple[ReportItem, ...]


class DailyReportService:
    def __init__(self, connection: sqlite3.Connection): self.connection = connection

    def build(self, *, generated_at: datetime, days: int = 1, max_papers: int = 3, max_news: int = 4, max_opinions: int = 2, max_engineering: int = 2) -> DailyReport:
        since = generated_at - timedelta(days=max(1, days))
        rows = self.connection.execute(
            """SELECT * FROM research_items WHERE first_discovered_at>=?
               AND status!='archived' ORDER BY first_discovered_at DESC,id""",
            (since.isoformat(),),
        ).fetchall()
        items = []
        for row in rows:
            topics = tuple(value["topic_id"] for value in self.connection.execute(
                "SELECT topic_id FROM item_topics WHERE item_id=? ORDER BY confidence DESC,topic_id", (row["id"],),
            ))
            metadata = json.loads(row["metadata_json"] or "{}")
            evidence = self.connection.execute(
                "SELECT evidence_json FROM item_sources WHERE item_id=? ORDER BY last_seen_at DESC LIMIT 1", (row["id"],),
            ).fetchone()
            source_metadata = json.loads(evidence["evidence_json"] or "{}") if evidence else {}
            items.append(ReportItem(
                row["id"], row["title"], row["summary"], row["url"], row["item_type"],
                row["first_discovered_at"], topics,
                str(metadata.get("evidence_level") or ""),
                str(source_metadata.get("publisher_domain") or ""),
            ))
        take = lambda kinds, limit: tuple(item for item in items if item.item_type in kinds)[:limit]
        return DailyReport(
            generated_at, since,
            take({"paper", "newsletter_item"}, max_papers),
            take({"research_news", "business_news", "product_release", "research_signal"}, max_news),
            take({"opinion"}, max_opinions), take({"engineering", "code"}, max_engineering),
        )


def render_daily_report(report: DailyReport) -> str:
    lines = [f"# Research Daily · {report.generated_at.date().isoformat()}", ""]
    sections = (("Must-read papers", report.papers), ("Research and product news", report.news), ("Opinions to examine", report.opinions), ("Engineering and tools", report.engineering))
    for heading, items in sections:
        lines.extend((f"## {heading}", ""))
        if not items:
            lines.extend(("_No qualifying items._", "")); continue
        for item in items:
            badges = [item.item_type.replace("_", " ")]
            if item.topics: badges.append(", ".join(item.topics))
            if item.evidence_level: badges.append(item.evidence_level.replace("_", " "))
            if item.publisher_domain: badges.append(item.publisher_domain)
            lines.append(f"- [{item.title}]({item.url}) — {' · '.join(badges)}")
            if item.summary: lines.append(f"  {item.summary[:400].strip()}")
        lines.append("")
    return "\n".join(lines).rstrip()
