"""Health classification derived from persisted SourceRun facts."""

from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class SourceHealth:
    source_id: str
    display_name: str
    status: str
    runs: int
    successes: int
    failures: int
    fetched: int
    new: int
    merged: int
    filtered: int
    recommendations: int
    last_finished_at: str | None
    last_error: str | None
    consecutive_failures: int = 0
    cooldown_until: str | None = None
    incremental_cursor: str | None = None
    parser_empty_runs: int = 0

    @property
    def success_rate(self) -> float:
        return self.successes / self.runs if self.runs else 0.0

    @property
    def recommendation_conversion(self) -> float:
        return self.recommendations / self.new if self.new else 0.0


@dataclass(frozen=True)
class HealthReport:
    generated_at: str
    window_days: int
    sources: tuple[SourceHealth, ...]
    source_saturation: dict[str, float]


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cursor_summary(runtime_state: sqlite3.Row | None) -> str | None:
    if runtime_state is None:
        return None
    try:
        state = json.loads(runtime_state["provider_state_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    if state.get("uid_validity") and state.get("last_uid") is not None:
        return f"imap-uid:{state['last_uid']}@{state['uid_validity']}"
    if state.get("etag") or state.get("last_modified"):
        return "http-validator"
    return None


def build_health_report(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    window_days: int = 7,
    stale_after_hours: int = 48,
    noisy_min_new: int = 20,
    noisy_max_conversion: float = 0.02,
    overfiltered_min_fetched: int = 20,
    overfiltered_min_ratio: float = 0.95,
) -> HealthReport:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    source_rows = connection.execute(
        "SELECT * FROM sources ORDER BY id"
    ).fetchall()
    provisional: list[dict] = []
    total_new = 0
    for source in source_rows:
        runs = connection.execute(
            """
            SELECT * FROM source_runs
            WHERE source_id = ? AND started_at >= ?
            ORDER BY started_at
            """,
            (source["id"], cutoff.isoformat()),
        ).fetchall()
        successes = sum(row["status"] == "success" for row in runs)
        failures = sum(row["status"] == "failed" for row in runs)
        fetched = sum(row["fetched_count"] for row in runs)
        new = sum(row["new_count"] for row in runs)
        merged = sum(row["merged_count"] for row in runs)
        filtered = sum(row["filtered_count"] for row in runs)
        parser_empty_runs = 0
        for run in runs:
            try:
                metrics = json.loads(run["metrics_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metrics = {}
            if (
                isinstance(metrics, dict)
                and int(metrics.get("parsed_entry_count") or 0) > 0
                and int(metrics.get("emitted_item_count") or 0) == 0
            ):
                parser_empty_runs += 1
        total_new += new
        recommendation_count = connection.execute(
            """
            SELECT count(DISTINCT recommendations.id)
            FROM recommendations
            JOIN item_sources ON item_sources.item_id = recommendations.item_id
            WHERE item_sources.source_id = ? AND recommendations.recommended_at >= ?
            """,
            (source["id"], cutoff.isoformat()),
        ).fetchone()[0]
        latest = runs[-1] if runs else None
        runtime_state = connection.execute(
            "SELECT * FROM source_runtime_state WHERE source_id = ?", (source["id"],)
        ).fetchone()
        provisional.append({
            "source": source, "runs": runs, "successes": successes,
            "failures": failures, "fetched": fetched, "new": new,
            "merged": merged, "filtered": filtered,
            "recommendations": recommendation_count, "latest": latest,
            "runtime_state": runtime_state,
            "parser_empty_runs": parser_empty_runs,
        })

    health: list[SourceHealth] = []
    saturation: dict[str, float] = {}
    for value in provisional:
        source = value["source"]
        saturation[source["id"]] = value["new"] / total_new if total_new else 0.0
        latest = value["latest"]
        last_finished = latest["finished_at"] if latest else None
        last_time = _parse(last_finished)
        runtime_state = value["runtime_state"]
        cooldown_until = runtime_state["cooldown_until"] if runtime_state else None
        cooldown_time = _parse(cooldown_until)
        if not source["enabled"]:
            status = "disabled"
        elif cooldown_time is not None and cooldown_time > now:
            status = "cooldown"
        elif last_time is None or now - last_time > timedelta(hours=stale_after_hours):
            status = "stale"
        elif value["failures"] and value["successes"] / len(value["runs"]) < 0.5:
            status = "degraded"
        elif value["parser_empty_runs"] and value["parser_empty_runs"] * 2 >= max(1, value["successes"]):
            status = "parser_degraded"
        elif (
            source["provider"] != "gmail_newsletter"
            and value["fetched"] >= overfiltered_min_fetched
            and value["filtered"] / value["fetched"] >= overfiltered_min_ratio
            and value["new"] + value["merged"] == 0
        ):
            status = "overfiltered"
        elif value["new"] >= noisy_min_new and value["recommendations"] / value["new"] < noisy_max_conversion:
            status = "noisy"
        elif value["fetched"] == 0:
            status = "quiet"
        else:
            status = "healthy"
        health.append(SourceHealth(
            source_id=source["id"], display_name=source["display_name"],
            status=status, runs=len(value["runs"]), successes=value["successes"],
            failures=value["failures"], fetched=value["fetched"], new=value["new"],
            merged=value["merged"], filtered=value["filtered"],
            recommendations=value["recommendations"], last_finished_at=last_finished,
            last_error=(latest["error_code"] or latest["error_message"]) if latest else None,
            consecutive_failures=int(runtime_state["consecutive_failures"]) if runtime_state else 0,
            cooldown_until=cooldown_until,
            incremental_cursor=_cursor_summary(runtime_state),
            parser_empty_runs=value["parser_empty_runs"],
        ))
    return HealthReport(
        generated_at=now.isoformat(), window_days=window_days,
        sources=tuple(health), source_saturation=saturation,
    )


def render_health_report(report: HealthReport) -> str:
    lines = [f"Research Library health — last {report.window_days} days"]
    for source in report.sources:
        cooldown = f", cooldown_until={source.cooldown_until}" if source.cooldown_until else ""
        cursor = f", cursor={source.incremental_cursor}" if source.incremental_cursor else ""
        parser = f", parser_empty_runs={source.parser_empty_runs}" if source.parser_empty_runs else ""
        lines.append(
            f"- {source.display_name} [{source.status}]: "
            f"runs={source.runs}, fetched={source.fetched}, new={source.new}, "
            f"merged={source.merged}, filtered={source.filtered}, "
            f"recommended={source.recommendations}{cooldown}{cursor}{parser}"
        )
    return "\n".join(lines)
