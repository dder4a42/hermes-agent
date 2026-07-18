"""Transactional persistence and merge behavior for Research Library items."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from urllib.parse import urlparse

from .identity import canonical_key, external_identifiers
from .models import (
    ResearchItemDraft,
    SourceEvidence,
    SourceRunCounts,
    TopicMatch,
    UpsertResult,
    utc_iso,
)


class LibraryRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def upsert_source(
        self,
        *,
        source_id: str,
        provider: str,
        display_name: str,
        source_type: str,
        tier: float,
        now: datetime,
        enabled: bool = True,
        config: dict | None = None,
    ) -> None:
        timestamp = utc_iso(now)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sources(
                    id, provider, display_name, source_type, tier, enabled,
                    config_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    provider=excluded.provider,
                    display_name=excluded.display_name,
                    source_type=excluded.source_type,
                    tier=excluded.tier,
                    enabled=excluded.enabled,
                    config_json=excluded.config_json,
                    updated_at=excluded.updated_at
                """,
                (
                    source_id, provider, display_name, source_type, tier,
                    int(enabled), json.dumps(config or {}, sort_keys=True),
                    timestamp, timestamp,
                ),
            )

    def record_newsletter_issue(
        self, *, source_id: str, mailbox: str, uid: str, body_hash: str,
        parser_id: str, created_at: datetime, entries: tuple[dict, ...],
        uid_validity: str = "", message_id: str = "", sender: str = "",
        subject: str = "", received_at: str | None = None,
        parse_status: str = "parsed",
    ) -> str:
        """Idempotently stage a parsed issue and its independently classified entries."""
        if parse_status not in {"parsed", "partial", "failed"}:
            raise ValueError(f"Invalid newsletter parse status: {parse_status}")
        issue_id = f"ni_{uuid.uuid4().hex}"
        with self.connection:
            existing = self.connection.execute(
                "SELECT id FROM newsletter_issues WHERE source_id=? AND mailbox=? AND uid_validity=? AND uid=?",
                (source_id, mailbox, uid_validity, uid),
            ).fetchone()
            if existing is not None:
                return str(existing["id"])
            self.connection.execute(
                """
                INSERT INTO newsletter_issues(
                    id, source_id, mailbox, uid_validity, uid, message_id, sender,
                    subject, received_at, body_hash, parser_id, parse_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (issue_id, source_id, mailbox, uid_validity, uid, message_id, sender,
                 subject, received_at, body_hash, parser_id, parse_status, utc_iso(created_at)),
            )
            for position, entry in enumerate(entries):
                canonical_url = str(entry.get("canonical_url") or "")
                self.connection.execute(
                    """
                    INSERT INTO newsletter_entries(
                        id, issue_id, position, section, title, excerpt, tracked_url,
                        canonical_url, publisher_domain, content_type,
                        classification_confidence, filter_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (f"ne_{uuid.uuid4().hex}", issue_id, position,
                     str(entry.get("section") or ""), str(entry.get("title") or ""),
                     str(entry.get("excerpt") or ""), str(entry.get("tracked_url") or ""),
                     canonical_url, (urlparse(canonical_url).hostname or "").lower(),
                     str(entry.get("content_type") or "unknown"),
                     max(0.0, min(1.0, float(entry.get("classification_confidence", .5)))),
                     str(entry.get("filter_reason") or "")),
                )
        return issue_id

    def start_source_run(
        self,
        *,
        source_id: str,
        started_at: datetime,
        run_id: str | None = None,
        metrics: dict | None = None,
    ) -> str:
        run_id = run_id or f"sr_{uuid.uuid4().hex}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO source_runs(
                    id, source_id, started_at, status, metrics_json
                ) VALUES (?, ?, ?, 'running', ?)
                """,
                (
                    run_id,
                    source_id,
                    utc_iso(started_at),
                    json.dumps(metrics or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
        return run_id

    def finish_source_run(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: datetime,
        counts: SourceRunCounts = SourceRunCounts(),
        error_code: str | None = None,
        error_message: str | None = None,
        metrics: dict | None = None,
    ) -> None:
        if status not in {"success", "partial", "failed"}:
            raise ValueError(f"Invalid terminal source-run status: {status}")
        if status == "success" and (error_code or error_message):
            raise ValueError("Successful source runs cannot carry an error")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE source_runs SET
                    finished_at = ?, status = ?, requests = ?, fetched_count = ?,
                    new_count = ?, merged_count = ?, unchanged_count = ?,
                    filtered_count = ?, error_code = ?, error_message = ?,
                    metrics_json = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    utc_iso(finished_at), status, counts.requests, counts.fetched,
                    counts.new, counts.merged, counts.unchanged, counts.filtered,
                    error_code, error_message,
                    json.dumps(metrics or {}, ensure_ascii=False, sort_keys=True),
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Source run is missing or already finished: {run_id}")

    def record_recommendation(
        self,
        item_id: str,
        *,
        score: float,
        score_breakdown: dict,
        recommended_at: datetime,
        rationale: str = "",
        recommendation_id: str | None = None,
    ) -> str:
        recommendation_id = recommendation_id or f"rec_{uuid.uuid4().hex}"
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE research_items SET status = 'recommended' WHERE id = ?",
                (item_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown Research Item: {item_id}")
            self.connection.execute(
                """
                INSERT INTO recommendations(
                    id, item_id, recommended_at, score,
                    score_breakdown_json, rationale
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    recommendation_id, item_id, utc_iso(recommended_at), score,
                    json.dumps(score_breakdown, ensure_ascii=False, sort_keys=True),
                    rationale,
                ),
            )
        return recommendation_id

    def record_feedback(
        self,
        item_id: str,
        *,
        kind: str,
        created_at: datetime,
        payload: dict | None = None,
        event_id: str | None = None,
    ) -> str:
        status_by_kind = {
            "save": "saved",
            "read": "read",
            "skip": "skipped",
            "archive": "archived",
        }
        if kind not in {*status_by_kind, "note"}:
            raise ValueError(f"Unsupported feedback kind: {kind}")
        event_id = event_id or f"fb_{uuid.uuid4().hex}"
        with self.connection:
            row = self.connection.execute(
                "SELECT 1 FROM research_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown Research Item: {item_id}")
            self.connection.execute(
                """
                INSERT INTO feedback_events(id, item_id, kind, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_id, item_id, kind, utc_iso(created_at),
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            if status := status_by_kind.get(kind):
                self.connection.execute(
                    "UPDATE research_items SET status = ? WHERE id = ?",
                    (status, item_id),
                )
        return event_id

    def upsert_item(
        self,
        draft: ResearchItemDraft,
        *,
        source: SourceEvidence,
        topics: tuple[TopicMatch, ...] = (),
        discovered_at: datetime,
    ) -> UpsertResult:
        key = canonical_key(draft)
        timestamp = utc_iso(discovered_at)
        identifiers = external_identifiers(draft)
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM research_items WHERE canonical_key = ?", (key,)
            ).fetchone()
            if row is None:
                for scheme, value in identifiers.items():
                    row = self.connection.execute(
                        """
                        SELECT research_items.*
                        FROM item_identifiers
                        JOIN research_items ON research_items.id = item_identifiers.item_id
                        WHERE item_identifiers.scheme = ? AND item_identifiers.value = ?
                        """,
                        (scheme, value),
                    ).fetchone()
                    if row is not None:
                        break
            disposition = "new"
            if row is None:
                item_id = f"ri_{uuid.uuid4().hex}"
                self.connection.execute(
                    """
                    INSERT INTO research_items(
                        id, canonical_key, item_type, title, summary, url,
                        authors_json, published_at, first_discovered_at,
                        last_seen_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id, key, draft.item_type, draft.title.strip(),
                        draft.summary.strip(), draft.url.strip(),
                        json.dumps(list(draft.authors), ensure_ascii=False),
                        draft.published_at, timestamp, timestamp,
                        json.dumps(draft.metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
            else:
                item_id = row["id"]
                key = row["canonical_key"]
                disposition = "merged"
                summary = draft.summary.strip() if len(draft.summary.strip()) > len(row["summary"]) else row["summary"]
                authors_json = json.dumps(list(draft.authors), ensure_ascii=False) if len(draft.authors) > len(json.loads(row["authors_json"])) else row["authors_json"]
                self.connection.execute(
                    """
                    UPDATE research_items SET
                        title = CASE WHEN length(?) > length(title) THEN ? ELSE title END,
                        summary = ?,
                        url = CASE WHEN url = '' THEN ? ELSE url END,
                        authors_json = ?,
                        published_at = COALESCE(published_at, ?),
                        last_seen_at = ?
                    WHERE id = ?
                    """,
                    (draft.title.strip(), draft.title.strip(), summary, draft.url.strip(), authors_json, draft.published_at, timestamp, item_id),
                )

            for scheme, value in identifiers.items():
                owner = self.connection.execute(
                    "SELECT item_id FROM item_identifiers WHERE scheme = ? AND value = ?",
                    (scheme, value),
                ).fetchone()
                if owner is not None and owner["item_id"] != item_id:
                    raise ValueError(f"Identifier {scheme}:{value} belongs to another Research Item")
                self.connection.execute(
                    "INSERT OR IGNORE INTO item_identifiers(item_id, scheme, value) VALUES (?, ?, ?)",
                    (item_id, scheme, value),
                )

            previous_source = self.connection.execute(
                "SELECT 1 FROM item_sources WHERE item_id = ? AND source_id = ?",
                (item_id, source.source_id),
            ).fetchone()
            self.connection.execute(
                """
                INSERT INTO item_sources(
                    item_id, source_id, first_seen_at, last_seen_at,
                    discovery_count, last_source_run_id, evidence_json
                ) VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(item_id, source_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    discovery_count=item_sources.discovery_count + 1,
                    last_source_run_id=excluded.last_source_run_id,
                    evidence_json=excluded.evidence_json
                """,
                (
                    item_id, source.source_id, timestamp, timestamp,
                    source.source_run_id,
                    json.dumps({"query": source.query, "rank": source.rank, **source.metadata}, ensure_ascii=False, sort_keys=True),
                ),
            )
            if row is not None and previous_source is not None and not topics:
                disposition = "unchanged"

            for topic in topics:
                self.connection.execute(
                    """
                    INSERT INTO item_topics(item_id, topic_id, confidence, matched_terms_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(item_id, topic_id) DO UPDATE SET
                        confidence=max(item_topics.confidence, excluded.confidence),
                        matched_terms_json=excluded.matched_terms_json
                    """,
                    (
                        item_id, topic.topic_id, topic.confidence,
                        json.dumps(list(topic.matched_terms), ensure_ascii=False),
                    ),
                )
        return UpsertResult(item_id=item_id, canonical_key=key, disposition=disposition)

    def classify_item(
        self,
        draft: ResearchItemDraft,
        *,
        source_id: str,
        topics: tuple[TopicMatch, ...] = (),
    ) -> str:
        """Classify a prospective upsert without mutating the library."""
        key = canonical_key(draft)
        row = self.connection.execute(
            "SELECT id FROM research_items WHERE canonical_key = ?", (key,)
        ).fetchone()
        if row is None:
            for scheme, value in external_identifiers(draft).items():
                row = self.connection.execute(
                    "SELECT item_id AS id FROM item_identifiers WHERE scheme = ? AND value = ?",
                    (scheme, value),
                ).fetchone()
                if row is not None:
                    break
        if row is None:
            return "new"
        item_id = row["id"]
        has_source = self.connection.execute(
            "SELECT 1 FROM item_sources WHERE item_id = ? AND source_id = ?",
            (item_id, source_id),
        ).fetchone()
        existing_topics = {
            record["topic_id"]
            for record in self.connection.execute(
                "SELECT topic_id FROM item_topics WHERE item_id = ?", (item_id,)
            )
        }
        if has_source is None or any(topic.topic_id not in existing_topics for topic in topics):
            return "merged"
        return "unchanged"

    def list_ranking_records(self, *, status: str = "discovered") -> list[dict]:
        """Return storage-neutral records consumed by the ranking service."""
        records: list[dict] = []
        rows = self.connection.execute(
            "SELECT * FROM research_items WHERE status = ? ORDER BY first_discovered_at, id",
            (status,),
        ).fetchall()
        for row in rows:
            identifiers = {
                value["scheme"]: value["value"]
                for value in self.connection.execute(
                    "SELECT scheme, value FROM item_identifiers WHERE item_id = ?",
                    (row["id"],),
                )
            }
            source_tiers = {
                value["source_id"]: float(value["tier"])
                for value in self.connection.execute(
                    """
                    SELECT item_sources.source_id, sources.tier
                    FROM item_sources JOIN sources ON sources.id = item_sources.source_id
                    WHERE item_sources.item_id = ?
                    """,
                    (row["id"],),
                )
            }
            topic_confidences = {
                value["topic_id"]: float(value["confidence"])
                for value in self.connection.execute(
                    "SELECT topic_id, confidence FROM item_topics WHERE item_id = ?",
                    (row["id"],),
                )
            }
            records.append({
                "id": row["id"],
                "title": row["title"],
                "summary": row["summary"],
                "url": row["url"],
                "item_type": row["item_type"],
                "published_at": row["published_at"],
                "identifiers": identifiers,
                "source_tiers": source_tiers,
                "topic_confidences": topic_confidences,
            })
        return records
