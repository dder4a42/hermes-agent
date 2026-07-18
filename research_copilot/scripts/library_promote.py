"""Cron shim for topic-gated newsletter promotion."""

from __future__ import annotations

import argparse


def main() -> int:
    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    return cmd_research_copilot(argparse.Namespace(
        research_copilot_command="promote-newsletters", dry_run=False, limit=100,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
