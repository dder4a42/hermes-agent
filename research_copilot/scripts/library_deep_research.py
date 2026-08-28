"""Cron shim for one bounded, web-only Library deep-research run."""

from __future__ import annotations

import argparse
from datetime import datetime


def reconcile_previous_delivery() -> str | None:
    """Acknowledge the previous cron receipt before choosing another paper."""
    from cron.jobs import list_jobs
    from research_copilot.runtime import open_library, runtime_paths

    jobs = {
        str(value.get("name")): value for value in list_jobs(include_disabled=True)
        if value.get("name") in {
            "research-weekly-deep-research", "research-deep-delivery-retry",
        }
    }
    connection, repository = open_library(runtime_paths()["database"])
    try:
        reconciled = None
        for job in sorted(jobs.values(), key=lambda value: str(value.get("last_run_at") or "")):
            if not job.get("last_run_at") or not job.get("last_status"):
                continue
            completed_at = datetime.fromisoformat(
                str(job["last_run_at"]).replace("Z", "+00:00")
            )
            delivery_error = str(job.get("last_delivery_error") or "")
            delivered = job.get("last_status") == "ok" and not delivery_error
            error = delivery_error or str(job.get("last_error") or "")
            result = repository.reconcile_deep_research_delivery_attempt(
                completed_at=completed_at, delivered=delivered, error=error,
            )
            reconciled = result or reconciled
        return reconciled
    finally:
        connection.close()


def main() -> int:
    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    reconcile_previous_delivery()
    return cmd_research_copilot(argparse.Namespace(
        research_copilot_command="deep-research",
        research_deep_research_command="run",
        item_id=None,
        dry_run=False,
        timeout=None,
        no_export=False,
        delivery_context=True,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
