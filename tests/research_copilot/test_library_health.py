from __future__ import annotations

from datetime import datetime, timedelta, timezone

from research_copilot.health import build_health_report, render_health_report
from research_copilot.library import LibraryRepository, SourceRunCounts, connect_library, initialize_library


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def _setup(tmp_path):
    connection = connect_library(tmp_path / "library.db")
    initialize_library(connection, migrated_at=NOW.isoformat())
    return connection, LibraryRepository(connection)


def _source(repo, source_id, *, enabled=True):
    repo.upsert_source(
        source_id=source_id, provider=source_id, display_name=source_id.upper(),
        source_type="test", tier=0.8, enabled=enabled, now=NOW,
    )


def _run(repo, source_id, *, status="success", when=NOW, fetched=5, new=2):
    run_id = repo.start_source_run(source_id=source_id, started_at=when)
    repo.finish_source_run(
        run_id, status=status, finished_at=when,
        counts=SourceRunCounts(requests=1, fetched=fetched, new=new),
        error_code="failed" if status == "failed" else None,
    )


def test_health_classifies_disabled_stale_quiet_degraded_and_healthy(tmp_path):
    connection, repo = _setup(tmp_path)
    try:
        _source(repo, "disabled", enabled=False)
        _source(repo, "stale")
        _run(repo, "stale", when=NOW - timedelta(days=4))
        _source(repo, "quiet")
        _run(repo, "quiet", fetched=0, new=0)
        _source(repo, "degraded")
        _run(repo, "degraded", status="failed", fetched=0, new=0)
        _source(repo, "healthy")
        _run(repo, "healthy")
        report = build_health_report(connection, now=NOW)
        assert {source.source_id: source.status for source in report.sources} == {
            "degraded": "degraded", "disabled": "disabled", "healthy": "healthy",
            "quiet": "quiet", "stale": "stale",
        }
        assert "HEALTHY [healthy]" in render_health_report(report)
    finally:
        connection.close()


def test_health_marks_high_volume_zero_conversion_source_noisy(tmp_path):
    connection, repo = _setup(tmp_path)
    try:
        _source(repo, "noisy")
        _run(repo, "noisy", fetched=100, new=80)
        report = build_health_report(connection, now=NOW)
        source = report.sources[0]
        assert source.status == "noisy"
        assert source.recommendation_conversion == 0.0
        assert report.source_saturation == {"noisy": 1.0}
    finally:
        connection.close()


def test_source_saturation_uses_new_items_not_fetched_items(tmp_path):
    connection, repo = _setup(tmp_path)
    try:
        _source(repo, "a")
        _source(repo, "b")
        _run(repo, "a", fetched=100, new=3)
        _run(repo, "b", fetched=5, new=1)
        report = build_health_report(connection, now=NOW, noisy_min_new=999)
        assert report.source_saturation == {"a": 0.75, "b": 0.25}
    finally:
        connection.close()
