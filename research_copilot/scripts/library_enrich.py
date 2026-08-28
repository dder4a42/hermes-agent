"""Cron shim for bounded newsletter URL and metadata enrichment."""

from __future__ import annotations

import argparse


def main() -> int:
    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    return cmd_research_copilot(argparse.Namespace(
        research_copilot_command="enrich-newsletters", dry_run=False, limit=50,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
