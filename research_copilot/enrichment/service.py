"""Database orchestration for cached newsletter URL and page enrichment."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .page_metadata import PageMetadataExtractor
from .url_resolver import UrlResolutionError, UrlResolver


@dataclass(frozen=True)
class EnrichmentSummary:
    selected: int = 0
    resolved: int = 0
    metadata_fetched: int = 0
    failed: int = 0
    dry_run: bool = False


class NewsletterEnrichmentService:
    def __init__(self, connection: sqlite3.Connection, *, resolver=None, metadata_extractor=None):
        self.connection = connection
        self.resolver = resolver or UrlResolver()
        self.metadata_extractor = metadata_extractor or PageMetadataExtractor()

    def enrich(self, *, limit: int = 50, dry_run: bool = False, now: datetime | None = None) -> EnrichmentSummary:
        now = now or datetime.now(timezone.utc)
        retry_before = (now - timedelta(hours=6)).isoformat()
        rows = self.connection.execute(
            """
            SELECT e.* FROM newsletter_entries e
            JOIN newsletter_issues i ON i.id=e.issue_id
            WHERE e.filter_reason='' AND e.research_item_id IS NULL
              AND (e.resolution_status='pending' OR
                   (e.resolution_status='failed' AND
                    (e.resolved_at IS NULL OR e.resolved_at<=?)))
            ORDER BY CAST(i.uid AS INTEGER), e.position LIMIT ?
            """, (retry_before, max(0, limit)),
        ).fetchall()
        resolved = metadata_count = failed = 0
        for row in rows:
            input_url = row["canonical_url"] or row["tracked_url"]
            try:
                outcome = self.resolver.resolve(input_url)
                resolved += 1
                metadata = None
                if 200 <= outcome.status_code < 300 and "html" in outcome.content_type.lower():
                    metadata = self.metadata_extractor.fetch(outcome.final_url)
                    metadata_count += 1
                if not dry_run:
                    with self.connection:
                        self.connection.execute(
                            """INSERT INTO url_resolutions(input_url, final_url, redirect_chain_json,
                               status_code, content_type, resolved_at, expires_at, error_code, attempt_count)
                               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1)
                               ON CONFLICT(input_url) DO UPDATE SET final_url=excluded.final_url,
                               redirect_chain_json=excluded.redirect_chain_json, status_code=excluded.status_code,
                               content_type=excluded.content_type, resolved_at=excluded.resolved_at,
                               expires_at=excluded.expires_at, error_code=NULL,
                               attempt_count=url_resolutions.attempt_count+1""",
                            (input_url, outcome.final_url, json.dumps(outcome.redirect_chain), outcome.status_code,
                             outcome.content_type, now.isoformat(), (now + timedelta(days=30)).isoformat()),
                        )
                        self.connection.execute(
                            "UPDATE newsletter_entries SET canonical_url=?, publisher_domain=?, resolution_status='resolved', resolution_error=NULL, resolved_at=? WHERE id=?",
                            (outcome.final_url, __import__("urllib.parse", fromlist=["urlparse"]).urlparse(outcome.final_url).hostname or "", now.isoformat(), row["id"]),
                        )
                        if metadata is not None:
                            self.connection.execute(
                                """INSERT INTO page_metadata(canonical_url,title,description,author,publisher,
                                   published_at,page_type,identifiers_json,extraction_method,fetched_at,content_hash,fetch_status)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?,'success')
                                   ON CONFLICT(canonical_url) DO UPDATE SET title=excluded.title,
                                   description=excluded.description,author=excluded.author,publisher=excluded.publisher,
                                   published_at=excluded.published_at,page_type=excluded.page_type,
                                   identifiers_json=excluded.identifiers_json,extraction_method=excluded.extraction_method,
                                   fetched_at=excluded.fetched_at,content_hash=excluded.content_hash,fetch_status='success'""",
                                (metadata.canonical_url, metadata.title, metadata.description, metadata.author,
                                 metadata.publisher, metadata.published_at, metadata.page_type,
                                 json.dumps(metadata.identifiers or {}, sort_keys=True), metadata.extraction_method,
                                 now.isoformat(), metadata.content_hash),
                            )
            except (UrlResolutionError, ValueError, OSError) as exc:
                failed += 1
                if not dry_run:
                    code = getattr(exc, "code", "metadata_error")
                    with self.connection:
                        self.connection.execute(
                            "UPDATE newsletter_entries SET resolution_status='failed', resolution_error=?, resolved_at=? WHERE id=?",
                            (f"{code}: {exc}"[:1000], now.isoformat(), row["id"]),
                        )
        return EnrichmentSummary(len(rows), resolved, metadata_count, failed, dry_run)
