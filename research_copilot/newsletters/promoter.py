"""Promote enriched staging entries into canonical Research Items."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from research_copilot.library import LibraryRepository, ResearchItemDraft, SourceEvidence, TopicMatch
from research_copilot.sources.topic_matching import match_topic_content, significant_tokens


@dataclass(frozen=True)
class PromotionSummary:
    selected: int = 0
    new: int = 0
    merged: int = 0
    skipped: int = 0
    dry_run: bool = False


class NewsletterPromoter:
    ALLOWED_TYPES = {"paper", "research_news", "business_news", "product_release", "engineering", "opinion"}

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.repository = LibraryRepository(connection)

    def promote(self, *, source_id: str = "gmail-newsletters", limit: int = 50,
                dry_run: bool = False, now: datetime,
                topic_policies: dict[str, dict[str, tuple[str, ...]]] | None = None) -> PromotionSummary:
        rows = self.connection.execute(
            """SELECT e.*, i.subject AS issue_subject, i.id AS newsletter_issue_id,
                      m.title AS page_title, m.description AS page_description,
                      m.published_at AS page_published_at, m.identifiers_json
               FROM newsletter_entries e JOIN newsletter_issues i ON i.id=e.issue_id
               LEFT JOIN page_metadata m ON m.canonical_url=e.canonical_url
               WHERE e.filter_reason='' AND e.research_item_id IS NULL
                 AND e.resolution_status='resolved' ORDER BY i.created_at,e.position LIMIT ?""",
            (max(0, limit),),
        ).fetchall()
        new = merged = skipped = 0
        for row in rows:
            if row["content_type"] not in self.ALLOWED_TYPES:
                skipped += 1; continue
            identifiers = json.loads(row["identifiers_json"] or "{}")
            draft = ResearchItemDraft(
                title=row["page_title"] or row["title"], item_type=row["content_type"],
                summary=row["page_description"] or row["excerpt"], url=row["canonical_url"],
                published_at=row["page_published_at"], arxiv_id=str(identifiers.get("arxiv") or ""),
                doi=str(identifiers.get("doi") or ""),
                metadata={"newsletter_issue_id": row["newsletter_issue_id"], "newsletter_entry_id": row["id"], "newsletter_subject": row["issue_subject"], "evidence_level": self._evidence_level(row["content_type"])},
            )
            topics = self._match_topics(draft, topic_policies or {})
            if topic_policies and not topics:
                skipped += 1; continue
            disposition = self.repository.classify_item(draft, source_id=source_id, topics=topics)
            if dry_run:
                item_id = None
            else:
                outcome = self.repository.upsert_item(
                    draft, source=SourceEvidence(source_id=source_id, metadata={"newsletter_issue_id": row["newsletter_issue_id"], "newsletter_entry_id": row["id"], "publisher_domain": row["publisher_domain"]}),
                    topics=topics, discovered_at=now,
                )
                disposition = outcome.disposition; item_id = outcome.item_id
                with self.connection:
                    self.connection.execute("UPDATE newsletter_entries SET research_item_id=? WHERE id=?", (item_id, row["id"]))
            if disposition == "new": new += 1
            elif disposition in {"merged", "unchanged"}: merged += 1
        return PromotionSummary(len(rows), new, merged, skipped, dry_run)

    @staticmethod
    def _evidence_level(content_type: str) -> str:
        return {"paper": "primary_research", "opinion": "commentary", "product_release": "official_or_secondary_announcement"}.get(content_type, "secondary_summary")

    @staticmethod
    def _match_topics(
        draft: ResearchItemDraft,
        policies: dict[str, dict[str, tuple[str, ...]]],
    ) -> tuple[TopicMatch, ...]:
        text = f"{draft.title} {draft.summary}".casefold()
        tokens = set(significant_tokens(text))
        matches = []
        for topic_id, policy in policies.items():
            terms = tuple(term for term in policy.get("include", ()) if term)
            evidence = match_topic_content(
                text, include_terms=terms, exclude_terms=policy.get("exclude", ()),
            )
            if evidence.excluded_by:
                continue
            hits = list(evidence.hits)
            if not hits and NewsletterPromoter._type_fallback(topic_id, draft.item_type, tokens):
                hits.append(f"{draft.item_type}:agent")
            if hits:
                matches.append(TopicMatch(topic_id, min(1.0, .5 + .1 * (len(hits) - 1)), tuple(hits)))
        return tuple(matches)

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return set(significant_tokens(text))

    @staticmethod
    def _type_fallback(topic_id: str, item_type: str, tokens: set[str]) -> bool:
        if topic_id == "agent-infra" and item_type in {"engineering", "product_release"}:
            return "agent" in tokens
        if topic_id == "research-agent":
            return {"research", "agent"} <= tokens
        if topic_id == "evaluation-harness":
            return "benchmark" in tokens and bool({"agent", "evaluation"} & tokens)
        if topic_id == "long-horizon-agent":
            return "agent" in tokens and bool({"trajectory", "memory", "planning", "horizon"} & tokens)
        return False
