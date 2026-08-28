"""Health classification derived from persisted SourceRun facts."""

from __future__ import annotations

import sqlite3
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


def build_health_report(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    window_days: int = 7,
    stale_after_hours: int = 48,
    noisy_min_new: int = 20,
    noisy_max_conversion: float = 0.02,
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
        provisional.append({
            "source": source, "runs": runs, "successes": successes,
            "failures": failures, "fetched": fetched, "new": new,
            "merged": merged, "filtered": filtered,
            "recommendations": recommendation_count, "latest": latest,
        })

    health: list[SourceHealth] = []
    saturation: dict[str, float] = {}
    for value in provisional:
        source = value["source"]
        saturation[source["id"]] = value["new"] / total_new if total_new else 0.0
        latest = value["latest"]
        last_finished = latest["finished_at"] if latest else None
        last_time = _parse(last_finished)
        if not source["enabled"]:
            status = "disabled"
        elif last_time is None or now - last_time > timedelta(hours=stale_after_hours):
            status = "stale"
        elif value["failures"] and value["successes"] / len(value["runs"]) < 0.5:
            status = "degraded"
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
        ))
    return HealthReport(
        generated_at=now.isoformat(), window_days=window_days,
        sources=tuple(health), source_saturation=saturation,
    )


def render_health_report(report: HealthReport) -> str:
    lines = [f"Research Library health — last {report.window_days} days"]
    for source in report.sources:
        lines.append(
            f"- {source.display_name} [{source.status}]: "
            f"runs={source.runs}, fetched={source.fetched}, new={source.new}, "
            f"merged={source.merged}, filtered={source.filtered}, "
            f"recommended={source.recommendations}"
        )
    return "\n".join(lines)
