"""Transactional persistence and merge behavior for Research Library items."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse

from .identity import canonical_key, external_identifiers, normalize_title
from .models import (
    ResearchItemDraft,
    SourceEvidence,
    SourceRunCounts,
    TopicMatch,
    UpsertResult,
    utc_iso,
)


_WORKFLOW_TRANSITIONS = {
    "discovered": {"shortlisted", "reading", "synthesized", "rejected", "archived"},
    "shortlisted": {"shortlisted", "reading", "synthesized", "rejected", "archived"},
    "reading": {"reading", "synthesized", "rejected", "archived"},
    "synthesized": {"synthesized", "archived"},
    "rejected": {"rejected", "shortlisted", "archived"},
    "archived": {"archived", "shortlisted"},
}

_ANALYSIS_TRANSITIONS = {
    "none": {"none", "triaged", "deep_researched", "synthesized"},
    "triaged": {"triaged", "deep_researched", "synthesized"},
    "deep_researched": {"deep_researched", "synthesized"},
    "synthesized": {"synthesized"},
}


class LibraryRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @staticmethod
    def _year(value: str | None) -> str:
        import re

        match = re.search(r"\b(19|20)\d{2}\b", value or "")
        return match.group(0) if match else ""

    def _reconciliation_candidate(
        self,
        draft: ResearchItemDraft,
        identifiers: dict[str, str],
    ) -> tuple[sqlite3.Row | None, dict | None]:
        """Find one guarded title match; ambiguity or strong-ID conflict means no merge."""
        title = normalize_title(draft.title)
        if len(title) < 12:
            return None, None
        compatible_types = {"paper", "project", "research_signal"}
        incoming_authors = {normalize_title(author) for author in draft.authors if normalize_title(author)}
        incoming_year = self._year(draft.published_at)
        candidates: list[tuple[sqlite3.Row, dict]] = []
        for row in self.connection.execute(
            "SELECT * FROM research_items WHERE normalized_title=?", (title,)
        ):
            if draft.item_type not in compatible_types or row["item_type"] not in compatible_types:
                continue
            existing_identifiers = {
                value["scheme"]: value["value"] for value in self.connection.execute(
                    "SELECT scheme,value FROM item_identifiers WHERE item_id=?", (row["id"],)
                )
            }
            if any(
                scheme in identifiers and scheme in existing_identifiers
                and identifiers[scheme] != existing_identifiers[scheme]
                for scheme in ("doi", "arxiv", "semantic_scholar")
            ):
                continue
            existing_authors = {
                normalize_title(author) for author in json.loads(row["authors_json"])
                if normalize_title(author)
            }
            existing_year = self._year(row["published_at"])
            author_overlap = sorted(incoming_authors & existing_authors)
            same_year = bool(incoming_year and existing_year and incoming_year == existing_year)
            strong_identifier = any(
                scheme in identifiers or scheme in existing_identifiers
                for scheme in ("doi", "arxiv", "semantic_scholar")
            )
            if not author_overlap and not (same_year and strong_identifier):
                continue
            candidates.append((row, {
                "normalized_title": title,
                "author_overlap": author_overlap,
                "published_year": incoming_year if same_year else "",
                "incoming_identifiers": identifiers,
                "existing_identifiers": existing_identifiers,
            }))
        return candidates[0] if len(candidates) == 1 else (None, None)

    def _find_existing_item(
        self,
        draft: ResearchItemDraft,
        *,
        key: str,
        identifiers: dict[str, str],
    ) -> tuple[sqlite3.Row | None, dict | None]:
        row = self.connection.execute(
            "SELECT * FROM research_items WHERE canonical_key = ?", (key,)
        ).fetchone()
        if row is not None:
            return row, None
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
                return row, None
        return self._reconciliation_candidate(draft, identifiers)

    def merge_items(
        self, *, source_item_id: str, target_item_id: str,
        observed_at: datetime, reason: str,
    ) -> None:
        """Explicitly merge one verified duplicate identity into another.

        This is intentionally not an automatic title-only merge.  Callers must
        name both records and provide auditable evidence in ``reason``.
        """
        if source_item_id == target_item_id:
            raise ValueError("Source and target items must differ")
        source = self.connection.execute(
            "SELECT * FROM research_items WHERE id=?", (source_item_id,),
        ).fetchone()
        target = self.connection.execute(
            "SELECT * FROM research_items WHERE id=?", (target_item_id,),
        ).fetchone()
        if source is None or target is None:
            raise KeyError("Both source and target research items must exist")
        if normalize_title(source["title"]) != normalize_title(target["title"]):
            raise ValueError("Explicit merge requires equal normalized titles")
        reason = reason.strip()
        if not reason:
            raise ValueError("Explicit merge requires a non-empty evidence reason")

        source_metadata = json.loads(source["metadata_json"] or "{}")
        target_metadata = json.loads(target["metadata_json"] or "{}")
        alternate_urls = list(target_metadata.get("alternate_urls") or [])
        if source["url"] and source["url"] != target["url"] and source["url"] not in alternate_urls:
            alternate_urls.append(source["url"])
        if alternate_urls:
            target_metadata["alternate_urls"] = alternate_urls

        with self.connection:
            # Rows without composite uniqueness can be repointed directly.
            for table in (
                "item_identifiers", "item_merge_evidence", "recommendations",
                "recommendation_delivery_outbox", "deep_research_artifacts",
                "deep_research_delivery_outbox", "feedback_events", "evidence_records",
            ):
                self.connection.execute(
                    f"UPDATE {table} SET item_id=? WHERE item_id=?",
                    (target_item_id, source_item_id),
                )
            self.connection.execute(
                "UPDATE newsletter_entries SET research_item_id=? WHERE research_item_id=?",
                (target_item_id, source_item_id),
            )

            for row in self.connection.execute(
                "SELECT * FROM item_sources WHERE item_id=?", (source_item_id,),
            ).fetchall():
                existing = self.connection.execute(
                    "SELECT * FROM item_sources WHERE item_id=? AND source_id=?",
                    (target_item_id, row["source_id"]),
                ).fetchone()
                if existing is None:
                    self.connection.execute(
                        "UPDATE item_sources SET item_id=? WHERE item_id=? AND source_id=?",
                        (target_item_id, source_item_id, row["source_id"]),
                    )
                else:
                    self.connection.execute(
                        """UPDATE item_sources SET first_seen_at=?,last_seen_at=?,
                           discovery_count=?,last_source_run_id=?,evidence_json=?
                           WHERE item_id=? AND source_id=?""",
                        (
                            min(existing["first_seen_at"], row["first_seen_at"]),
                            max(existing["last_seen_at"], row["last_seen_at"]),
                            int(existing["discovery_count"]) + int(row["discovery_count"]),
                            row["last_source_run_id"] or existing["last_source_run_id"],
                            row["evidence_json"] if len(row["evidence_json"] or "") > len(existing["evidence_json"] or "") else existing["evidence_json"],
                            target_item_id, row["source_id"],
                        ),
                    )
                    self.connection.execute(
                        "DELETE FROM item_sources WHERE item_id=? AND source_id=?",
                        (source_item_id, row["source_id"]),
                    )

            for row in self.connection.execute(
                "SELECT * FROM item_topics WHERE item_id=?", (source_item_id,),
            ).fetchall():
                existing = self.connection.execute(
                    "SELECT * FROM item_topics WHERE item_id=? AND topic_id=?",
                    (target_item_id, row["topic_id"]),
                ).fetchone()
                if existing is None:
                    self.connection.execute(
                        "UPDATE item_topics SET item_id=? WHERE item_id=? AND topic_id=?",
                        (target_item_id, source_item_id, row["topic_id"]),
                    )
                else:
                    terms = list(dict.fromkeys(
                        [*json.loads(existing["matched_terms_json"] or "[]"),
                         *json.loads(row["matched_terms_json"] or "[]")]
                    ))
                    self.connection.execute(
                        "UPDATE item_topics SET confidence=?,matched_terms_json=? WHERE item_id=? AND topic_id=?",
                        (max(float(existing["confidence"]), float(row["confidence"])),
                         json.dumps(terms, ensure_ascii=False), target_item_id, row["topic_id"]),
                    )
                    self.connection.execute(
                        "DELETE FROM item_topics WHERE item_id=? AND topic_id=?",
                        (source_item_id, row["topic_id"]),
                    )

            self.connection.execute(
                """UPDATE research_items SET first_discovered_at=?,last_seen_at=?,
                   starred=?,metadata_json=? WHERE id=?""",
                (
                    min(target["first_discovered_at"], source["first_discovered_at"]),
                    max(target["last_seen_at"], source["last_seen_at"]),
                    max(int(target["starred"]), int(source["starred"])),
                    json.dumps(target_metadata, ensure_ascii=False, sort_keys=True),
                    target_item_id,
                ),
            )
            self.connection.execute(
                """INSERT INTO item_merge_evidence(
                   id,item_id,observed_at,method,incoming_canonical_key,evidence_json
                   ) VALUES (?,?,?,?,?,?)""",
                (
                    f"ime_{uuid.uuid4().hex}", target_item_id, utc_iso(observed_at),
                    "explicit_verified_duplicate", source["canonical_key"],
                    json.dumps({"source_item_id": source_item_id, "reason": reason}, ensure_ascii=False),
                ),
            )
            self.connection.execute("DELETE FROM research_items WHERE id=?", (source_item_id,))

    @staticmethod
    def _preferred_canonical_key(current: str, incoming: str) -> str:
        priority = {"doi": 0, "arxiv": 1, "semantic_scholar": 2, "url": 3, "title": 4}
        current_scheme = current.split(":", 1)[0]
        incoming_scheme = incoming.split(":", 1)[0]
        return incoming if priority.get(incoming_scheme, 99) < priority.get(current_scheme, 99) else current

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

    def get_source_runtime_state(self, source_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM source_runtime_state WHERE source_id = ?", (source_id,)
        ).fetchone()
        if row is None:
            return {
                "provider_state": {}, "consecutive_failures": 0,
                "cooldown_until": None, "last_error_code": None,
            }
        try:
            provider_state = json.loads(row["provider_state_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            provider_state = {}
        return {
            "provider_state": provider_state if isinstance(provider_state, dict) else {},
            "consecutive_failures": int(row["consecutive_failures"]),
            "cooldown_until": row["cooldown_until"],
            "last_error_code": row["last_error_code"],
            "updated_at": row["updated_at"],
        }

    def record_source_success(
        self,
        source_id: str,
        *,
        at: datetime,
        provider_state_updates: dict | None = None,
    ) -> None:
        existing = self.get_source_runtime_state(source_id)["provider_state"]
        existing.update(provider_state_updates or {})
        timestamp = utc_iso(at)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO source_runtime_state(
                    source_id, provider_state_json, consecutive_failures,
                    cooldown_until, last_error_code, updated_at
                ) VALUES (?, ?, 0, NULL, NULL, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    provider_state_json=excluded.provider_state_json,
                    consecutive_failures=0,
                    cooldown_until=NULL,
                    last_error_code=NULL,
                    updated_at=excluded.updated_at
                """,
                (source_id, json.dumps(existing, ensure_ascii=False, sort_keys=True), timestamp),
            )

    def record_source_failure(
        self,
        source_id: str,
        *,
        error_code: str,
        at: datetime,
        threshold: int,
        base_seconds: int,
        max_seconds: int,
    ) -> dict:
        state = self.get_source_runtime_state(source_id)
        failures = int(state["consecutive_failures"]) + 1
        cooldown_until = None
        if failures >= threshold:
            delay = base_seconds
            for _ in range(failures - threshold):
                delay = min(delay * 2, max_seconds)
                if delay == max_seconds:
                    break
            cooldown_until = utc_iso(at + timedelta(seconds=delay))
        timestamp = utc_iso(at)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO source_runtime_state(
                    source_id, provider_state_json, consecutive_failures,
                    cooldown_until, last_error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    consecutive_failures=excluded.consecutive_failures,
                    cooldown_until=excluded.cooldown_until,
                    last_error_code=excluded.last_error_code,
                    updated_at=excluded.updated_at
                """,
                (
                    source_id,
                    json.dumps(state["provider_state"], ensure_ascii=False, sort_keys=True),
                    failures, cooldown_until, error_code, timestamp,
                ),
            )
        return {"consecutive_failures": failures, "cooldown_until": cooldown_until}

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
            # 去重键：Message-ID（全局稳定）优先，回退 body_hash，再回退旧
            # (mailbox, uid_validity, uid) 键（schema UNIQUE 约束要求保留该键）。
            existing = None
            if message_id:
                existing = self.connection.execute(
                    "SELECT id FROM newsletter_issues WHERE source_id=? AND message_id=?",
                    (source_id, message_id),
                ).fetchone()
            if existing is None and body_hash:
                existing = self.connection.execute(
                    "SELECT id FROM newsletter_issues WHERE source_id=? AND body_hash=?",
                    (source_id, body_hash),
                ).fetchone()
            if existing is None and uid:
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
            row = self.connection.execute(
                "SELECT metrics_json FROM source_runs WHERE id = ? AND status = 'running'",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Source run is missing or already finished: {run_id}")
            merged_metrics = json.loads(row["metrics_json"] or "{}")
            merged_metrics.update(metrics or {})
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
                    json.dumps(merged_metrics, ensure_ascii=False, sort_keys=True),
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
        enqueue_delivery: bool = False,
    ) -> str:
        recommendation_id = recommendation_id or f"rec_{uuid.uuid4().hex}"
        with self.connection:
            if self.connection.execute(
                "SELECT 1 FROM research_items WHERE id = ?", (item_id,)
            ).fetchone() is None:
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
            if enqueue_delivery:
                self._enqueue_recommendation_delivery(
                    recommendation_id, item_id=item_id,
                    created_at=utc_iso(recommended_at),
                )
        return recommendation_id

    def record_recommendation_with_budget(
        self,
        item_id: str,
        *,
        score: float,
        score_breakdown: dict,
        recommended_at: datetime,
        rationale: str,
        daily_since: datetime,
        daily_limit: int,
        weekly_since: datetime,
        weekly_limit: int,
        recommendation_id: str | None = None,
        enqueue_delivery: bool = False,
    ) -> str | None:
        """Atomically insert a recommendation only while both budgets allow it."""
        recommendation_id = recommendation_id or f"rec_{uuid.uuid4().hex}"
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.connection.execute(
                "SELECT 1 FROM research_items WHERE id = ?", (item_id,),
            ).fetchone() is None:
                raise ValueError(f"Unknown Research Item: {item_id}")
            daily_used = int(self.connection.execute(
                "SELECT count(*) FROM recommendations WHERE recommended_at >= ?",
                (utc_iso(daily_since),),
            ).fetchone()[0])
            weekly_used = int(self.connection.execute(
                "SELECT count(*) FROM recommendations WHERE recommended_at >= ?",
                (utc_iso(weekly_since),),
            ).fetchone()[0])
            if daily_used >= daily_limit or weekly_used >= weekly_limit:
                self.connection.rollback()
                return None
            self.connection.execute(
                """
                INSERT INTO recommendations(
                    id, item_id, recommended_at, score, score_breakdown_json, rationale
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    recommendation_id, item_id, utc_iso(recommended_at), score,
                    json.dumps(score_breakdown, ensure_ascii=False, sort_keys=True), rationale,
                ),
            )
            if enqueue_delivery:
                self._enqueue_recommendation_delivery(
                    recommendation_id, item_id=item_id,
                    created_at=utc_iso(recommended_at),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return recommendation_id

    def _enqueue_recommendation_delivery(
        self,
        recommendation_id: str,
        *,
        item_id: str,
        created_at: str,
    ) -> str:
        outbox_id = f"outbox_{recommendation_id}"
        self.connection.execute(
            """INSERT INTO recommendation_delivery_outbox(
                   id,recommendation_id,item_id,created_at
               ) VALUES (?,?,?,?)""",
            (outbox_id, recommendation_id, item_id, created_at),
        )
        return outbox_id

    def recommendation_count_since(self, since: datetime) -> int:
        """Count recommendation events in a UTC time window."""
        row = self.connection.execute(
            "SELECT count(*) FROM recommendations WHERE recommended_at >= ?",
            (utc_iso(since),),
        ).fetchone()
        return int(row[0])

    def pending_delivery(self) -> dict | None:
        """Return the oldest unacknowledged recommendation delivery."""
        row = self.connection.execute(
            """SELECT recommendation_delivery_outbox.*,
                      recommendations.score, recommendations.score_breakdown_json,
                      recommendations.rationale, recommendations.recommended_at
               FROM recommendation_delivery_outbox
               JOIN recommendations
                 ON recommendations.id=recommendation_delivery_outbox.recommendation_id
               WHERE recommendation_delivery_outbox.status='pending'
               ORDER BY recommendation_delivery_outbox.created_at,
                        recommendation_delivery_outbox.id
               LIMIT 1"""
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json") or "{}")
        return value

    def set_delivery_payload(
        self,
        recommendation_id: str,
        *,
        payload: dict,
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE recommendation_delivery_outbox SET payload_json=?
                   WHERE recommendation_id=? AND status='pending'""",
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    recommendation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"Pending recommendation delivery not found: {recommendation_id}"
                )

    def mark_delivery_attempt(self, outbox_id: str, *, attempted_at: datetime) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE recommendation_delivery_outbox
                   SET attempt_count=attempt_count + 1, last_attempt_at=?, last_error=''
                   WHERE id=? AND status='pending'""",
                (utc_iso(attempted_at), outbox_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Pending delivery not found: {outbox_id}")

    def reconcile_delivery_attempt(
        self,
        *,
        completed_at: datetime,
        delivered: bool,
        error: str = "",
    ) -> str | None:
        """Apply one cron delivery receipt to the attempted pending message."""
        completed = utc_iso(completed_at)
        row = self.connection.execute(
            """SELECT id FROM recommendation_delivery_outbox
               WHERE status='pending' AND last_attempt_at IS NOT NULL
                 AND last_attempt_at <= ?
               ORDER BY last_attempt_at, id LIMIT 1""",
            (completed,),
        ).fetchone()
        if row is None:
            return None
        outbox_id = str(row["id"])
        with self.connection:
            if delivered:
                self.connection.execute(
                    """UPDATE recommendation_delivery_outbox
                       SET status='delivered', delivered_at=?, last_error=''
                       WHERE id=?""",
                    (completed, outbox_id),
                )
            else:
                self.connection.execute(
                    """UPDATE recommendation_delivery_outbox SET last_error=?
                       WHERE id=?""",
                    ((error or "delivery not confirmed").strip(), outbox_id),
                )
        return outbox_id

    def record_feedback(
        self,
        item_id: str,
        *,
        kind: str,
        created_at: datetime,
        payload: dict | None = None,
        event_id: str | None = None,
    ) -> str:
        workflow_by_kind = {
            "archive": "archived",
            "synthesize": "synthesized",
        }
        learning_by_kind = {
            "save": "saved",
            "start": "reading",
            "read": "read",
            "skip": "skipped",
            "synthesize": "read",
        }
        if kind not in {*workflow_by_kind, *learning_by_kind, "note"}:
            raise ValueError(f"Unsupported feedback kind: {kind}")
        event_id = event_id or f"fb_{uuid.uuid4().hex}"
        timestamp = utc_iso(created_at)
        with self.connection:
            row = self.connection.execute(
                """SELECT workflow_state, agent_analysis_status, user_learning_status
                   FROM research_items WHERE id = ?""",
                (item_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown Research Item: {item_id}")
            workflow_state = workflow_by_kind.get(kind)
            if workflow_state is not None:
                current = str(row["workflow_state"])
                if workflow_state not in _WORKFLOW_TRANSITIONS.get(current, set()):
                    raise ValueError(
                        f"Invalid Research Item workflow transition: {current} -> {workflow_state}"
                    )
            if (
                kind == "synthesize"
                and row["user_learning_status"] != "read"
                and row["agent_analysis_status"] != "deep_researched"
            ):
                raise ValueError(
                    "Synthesis requires a user-read or agent deep-researched item"
                )
            self.connection.execute(
                """
                INSERT INTO feedback_events(id, item_id, kind, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_id, item_id, kind, timestamp,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            learning_state = learning_by_kind.get(kind)
            if learning_state is not None:
                self.connection.execute(
                    """UPDATE research_items
                       SET user_learning_status=?, user_learning_updated_at=?
                       WHERE id=?""",
                    (learning_state, timestamp, item_id),
                )
            if kind == "synthesize":
                self.connection.execute(
                    """UPDATE research_items
                       SET agent_analysis_status='synthesized',
                           agent_analysis_updated_at=?
                       WHERE id=?""",
                    (timestamp, item_id),
                )
            if workflow_state is not None:
                self.connection.execute(
                    "UPDATE research_items SET workflow_state = ? WHERE id = ?",
                    (workflow_state, item_id),
                )
        return event_id

    def set_analysis_status(
        self,
        item_id: str,
        *,
        status: str,
        updated_at: datetime,
    ) -> None:
        """Advance machine analysis without claiming that the user read the item."""
        if status not in _ANALYSIS_TRANSITIONS:
            raise ValueError(f"Unsupported agent analysis status: {status}")
        timestamp = utc_iso(updated_at)
        with self.connection:
            row = self.connection.execute(
                "SELECT agent_analysis_status FROM research_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown Research Item: {item_id}")
            current = str(row["agent_analysis_status"])
            if status not in _ANALYSIS_TRANSITIONS[current]:
                raise ValueError(
                    f"Invalid agent analysis transition: {current} -> {status}"
                )
            self.connection.execute(
                """UPDATE research_items
                   SET agent_analysis_status=?, agent_analysis_updated_at=?
                   WHERE id=?""",
                (status, timestamp, item_id),
            )
            if status == "synthesized":
                self.connection.execute(
                    "UPDATE research_items SET workflow_state='synthesized' WHERE id=?",
                    (item_id,),
                )

    def import_deep_research_artifact(
        self,
        *,
        artifact_id: str,
        item_id: str,
        schema_version: int,
        generated_at: datetime,
        imported_at: datetime,
        research_question: str,
        content_hash: str,
        artifact: dict,
        producer: dict,
        delivery_payload: str | None = None,
    ) -> tuple[str, bool]:
        """Idempotently persist one validated artifact and advance analysis."""
        existing = self.connection.execute(
            "SELECT id,item_id,imported_at FROM deep_research_artifacts WHERE content_hash=?",
            (content_hash,),
        ).fetchone()
        if existing is not None:
            if delivery_payload is not None:
                with self.connection:
                    self._enqueue_deep_research_delivery(
                        str(existing["id"]), item_id=str(existing["item_id"]),
                        created_at=str(existing["imported_at"]), payload=delivery_payload,
                    )
            return str(existing["id"]), False
        with self.connection:
            item = self.connection.execute(
                "SELECT agent_analysis_status FROM research_items WHERE id=?",
                (item_id,),
            ).fetchone()
            if item is None:
                raise ValueError(f"Unknown Research Item: {item_id}")
            self.connection.execute(
                """INSERT INTO deep_research_artifacts(
                       id,item_id,schema_version,generated_at,imported_at,
                       research_question,content_hash,artifact_json,producer_json
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    artifact_id, item_id, schema_version, utc_iso(generated_at),
                    utc_iso(imported_at), research_question, content_hash,
                    json.dumps(artifact, ensure_ascii=False, sort_keys=True),
                    json.dumps(producer, ensure_ascii=False, sort_keys=True),
                ),
            )
            if item["agent_analysis_status"] in {"none", "triaged"}:
                self.connection.execute(
                    """UPDATE research_items
                       SET agent_analysis_status='deep_researched',
                           agent_analysis_updated_at=?
                       WHERE id=?""",
                    (utc_iso(imported_at), item_id),
                )
            if delivery_payload is not None:
                self._enqueue_deep_research_delivery(
                    artifact_id, item_id=item_id, created_at=utc_iso(imported_at),
                    payload=delivery_payload,
                )
        return artifact_id, True

    def _enqueue_deep_research_delivery(
        self, artifact_id: str, *, item_id: str, created_at: str, payload: str,
    ) -> str:
        if not payload.strip():
            raise ValueError("Deep-research delivery payload must not be empty")
        outbox_id = f"deep_outbox_{artifact_id}"
        self.connection.execute(
            """INSERT OR IGNORE INTO deep_research_delivery_outbox(
                   id,artifact_id,item_id,created_at,payload_text
               ) VALUES (?,?,?,?,?)""",
            (outbox_id, artifact_id, item_id, created_at, payload),
        )
        return outbox_id

    def pending_deep_research_delivery(self) -> dict | None:
        row = self.connection.execute(
            """SELECT deep_research_delivery_outbox.*, research_items.title,
                      research_items.url
               FROM deep_research_delivery_outbox
               JOIN research_items
                 ON research_items.id=deep_research_delivery_outbox.item_id
               WHERE deep_research_delivery_outbox.status='pending'
               ORDER BY deep_research_delivery_outbox.created_at,
                        deep_research_delivery_outbox.id LIMIT 1"""
        ).fetchone()
        return dict(row) if row is not None else None

    def mark_deep_research_delivery_attempt(
        self, outbox_id: str, *, attempted_at: datetime,
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE deep_research_delivery_outbox
                   SET attempt_count=attempt_count+1,last_attempt_at=?,last_error=''
                   WHERE id=? AND status='pending'""",
                (utc_iso(attempted_at), outbox_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Pending deep-research delivery not found: {outbox_id}")

    def reconcile_deep_research_delivery_attempt(
        self, *, completed_at: datetime, delivered: bool, error: str = "",
    ) -> str | None:
        completed = utc_iso(completed_at)
        row = self.connection.execute(
            """SELECT id FROM deep_research_delivery_outbox
               WHERE status='pending' AND last_attempt_at IS NOT NULL
                 AND last_attempt_at <= ?
               ORDER BY last_attempt_at,id LIMIT 1""",
            (completed,),
        ).fetchone()
        if row is None:
            return None
        outbox_id = str(row["id"])
        with self.connection:
            if delivered:
                self.connection.execute(
                    """UPDATE deep_research_delivery_outbox
                       SET status='delivered',delivered_at=?,last_error=''
                       WHERE id=?""",
                    (completed, outbox_id),
                )
            else:
                self.connection.execute(
                    "UPDATE deep_research_delivery_outbox SET last_error=? WHERE id=?",
                    ((error or "delivery not confirmed").strip(), outbox_id),
                )
        return outbox_id

    def enqueue_scout_delivery(
        self, *, artifact_path: str, payload: str, created_at: datetime,
    ) -> str:
        if not artifact_path.strip() or not payload.strip():
            raise ValueError("Scout delivery requires artifact_path and payload")
        digest = hashlib.sha256(artifact_path.encode("utf-8")).hexdigest()[:24]
        outbox_id = f"scout_outbox_{digest}"
        with self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO scout_delivery_outbox(
                       id,artifact_path,created_at,payload_text
                   ) VALUES (?,?,?,?)""",
                (outbox_id, artifact_path, utc_iso(created_at), payload),
            )
        return outbox_id

    def pending_scout_delivery(self) -> dict | None:
        row = self.connection.execute(
            """SELECT * FROM scout_delivery_outbox WHERE status='pending'
               ORDER BY created_at,id LIMIT 1"""
        ).fetchone()
        return dict(row) if row is not None else None

    def mark_scout_delivery_attempt(
        self, outbox_id: str, *, attempted_at: datetime,
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE scout_delivery_outbox
                   SET attempt_count=attempt_count+1,last_attempt_at=?,last_error=''
                   WHERE id=? AND status='pending'""",
                (utc_iso(attempted_at), outbox_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Pending Scout delivery not found: {outbox_id}")

    def reconcile_scout_delivery_attempt(
        self, *, completed_at: datetime, delivered: bool, error: str = "",
    ) -> str | None:
        completed = utc_iso(completed_at)
        row = self.connection.execute(
            """SELECT id FROM scout_delivery_outbox
               WHERE status='pending' AND last_attempt_at IS NOT NULL
                 AND last_attempt_at <= ?
               ORDER BY last_attempt_at,id LIMIT 1""",
            (completed,),
        ).fetchone()
        if row is None:
            return None
        outbox_id = str(row["id"])
        with self.connection:
            if delivered:
                self.connection.execute(
                    """UPDATE scout_delivery_outbox
                       SET status='delivered',delivered_at=?,last_error=''
                       WHERE id=?""",
                    (completed, outbox_id),
                )
            else:
                self.connection.execute(
                    "UPDATE scout_delivery_outbox SET last_error=? WHERE id=?",
                    ((error or "delivery not confirmed").strip(), outbox_id),
                )
        return outbox_id

    def record_evidence(
        self,
        item_id: str,
        *,
        created_at: datetime,
        agenda_id: str,
        belief_id: str | None,
        prompt_id: str | None,
        relation: str,
        claim_type: str,
        strength: float,
        source_quality: str,
        claim: str,
        rationale: str,
        profile_revision: str,
        evidence_id: str | None = None,
    ) -> str:
        if relation not in {"supports", "challenges", "contextualizes"}:
            raise ValueError(f"Unsupported evidence relation: {relation}")
        if claim_type not in {"source_claim", "agent_inference", "personal_take"}:
            raise ValueError(f"Unsupported evidence claim type: {claim_type}")
        if source_quality not in {"primary", "official", "secondary", "community", "unknown"}:
            raise ValueError(f"Unsupported evidence source quality: {source_quality}")
        if not 0 <= strength <= 1:
            raise ValueError("Evidence strength must be between 0 and 1")
        if not belief_id and not prompt_id:
            raise ValueError("Evidence must reference a belief or question/gap")
        if not claim.strip():
            raise ValueError("Evidence claim must not be empty")
        evidence_id = evidence_id or f"ev_{uuid.uuid4().hex}"
        with self.connection:
            item = self.connection.execute(
                """SELECT agent_analysis_status, user_learning_status
                   FROM research_items WHERE id = ?""",
                (item_id,),
            ).fetchone()
            if item is None:
                raise ValueError(f"Unknown Research Item: {item_id}")
            if (
                item["user_learning_status"] != "read"
                and item["agent_analysis_status"] not in {"deep_researched", "synthesized"}
            ):
                raise ValueError(
                    "Evidence requires a user-read or agent deep-researched item"
                )
            self.connection.execute(
                """
                INSERT INTO evidence_records(
                    id, item_id, created_at, agenda_id, belief_id, prompt_id,
                    relation, claim_type, strength, source_quality, claim,
                    rationale, profile_revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id, item_id, utc_iso(created_at), agenda_id,
                    belief_id, prompt_id, relation, claim_type, strength,
                    source_quality, claim.strip(), rationale.strip(), profile_revision,
                ),
            )
        return evidence_id

    def review_evidence(
        self,
        evidence_id: str,
        *,
        accepted: bool,
        reviewed_at: datetime,
        note: str = "",
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE evidence_records
                SET review_status=?, reviewed_at=?, review_note=?
                WHERE id=? AND review_status='pending'
                """,
                (
                    "accepted" if accepted else "rejected",
                    utc_iso(reviewed_at), note.strip(), evidence_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Evidence is missing or already reviewed: {evidence_id}")

    def list_evidence(self, *, review_status: str | None = None) -> list[dict]:
        if review_status is not None and review_status not in {"pending", "accepted", "rejected"}:
            raise ValueError(f"Unsupported evidence review status: {review_status}")
        where = "WHERE evidence_records.review_status = ?" if review_status else ""
        parameters = (review_status,) if review_status else ()
        return [dict(row) for row in self.connection.execute(
            f"""
            SELECT evidence_records.*, research_items.title, research_items.url
            FROM evidence_records
            JOIN research_items ON research_items.id = evidence_records.item_id
            {where}
            ORDER BY evidence_records.created_at DESC, evidence_records.id DESC
            """,
            parameters,
        )]

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
            row, reconciliation = self._find_existing_item(
                draft, key=key, identifiers=identifiers,
            )
            disposition = "new"
            if row is None:
                item_id = f"ri_{uuid.uuid4().hex}"
                self.connection.execute(
                    """
                    INSERT INTO research_items(
                        id, canonical_key, item_type, title, normalized_title, summary, url,
                        authors_json, published_at, first_discovered_at,
                        last_seen_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id, key, draft.item_type, draft.title.strip(),
                        normalize_title(draft.title),
                        draft.summary.strip(), draft.url.strip(),
                        json.dumps(list(draft.authors), ensure_ascii=False),
                        draft.published_at, timestamp, timestamp,
                        json.dumps(draft.metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
            else:
                item_id = row["id"]
                incoming_key = key
                key = self._preferred_canonical_key(row["canonical_key"], incoming_key)
                disposition = "merged"
                title = draft.title.strip() if len(draft.title.strip()) > len(row["title"]) else row["title"]
                summary = draft.summary.strip() if len(draft.summary.strip()) > len(row["summary"]) else row["summary"]
                authors_json = json.dumps(list(draft.authors), ensure_ascii=False) if len(draft.authors) > len(json.loads(row["authors_json"])) else row["authors_json"]
                metadata = {**json.loads(row["metadata_json"]), **draft.metadata}
                self.connection.execute(
                    """
                    UPDATE research_items SET
                        canonical_key = ?,
                        title = ?,
                        normalized_title = ?,
                        summary = ?,
                        url = CASE WHEN url = '' THEN ? ELSE url END,
                        authors_json = ?,
                        published_at = COALESCE(published_at, ?),
                        last_seen_at = ?,
                        metadata_json = ?
                    WHERE id = ?
                    """,
                    (key, title, normalize_title(title), summary,
                     draft.url.strip(), authors_json, draft.published_at,
                     timestamp, json.dumps(metadata, ensure_ascii=False, sort_keys=True), item_id),
                )
                if reconciliation is not None:
                    self.connection.execute(
                        """INSERT INTO item_merge_evidence(
                               id,item_id,observed_at,method,incoming_canonical_key,evidence_json
                           ) VALUES (?,?,?,?,?,?)""",
                        (
                            f"merge_{uuid.uuid4().hex}", item_id, timestamp,
                            "exact_title_with_author_or_year", incoming_key,
                            json.dumps(reconciliation, ensure_ascii=False, sort_keys=True),
                        ),
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
        identifiers = external_identifiers(draft)
        row, _reconciliation = self._find_existing_item(
            draft, key=key, identifiers=identifiers,
        )
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

    def list_ranking_records(self, *, workflow_state: str = "discovered") -> list[dict]:
        """Return storage-neutral records consumed by the ranking service."""
        records: list[dict] = []
        rows = self.connection.execute(
            """SELECT * FROM research_items
               WHERE workflow_state = ?
                 AND user_learning_status = 'unseen'
                 AND NOT EXISTS (
                     SELECT 1 FROM recommendations
                     WHERE recommendations.item_id = research_items.id
                 )
               ORDER BY first_discovered_at, id""",
            (workflow_state,),
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
