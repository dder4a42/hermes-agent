"""Cron shim that renders the local Research Library daily report."""

from __future__ import annotations

import argparse


def main() -> int:
    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    return cmd_research_copilot(argparse.Namespace(
        research_copilot_command="daily-report", days=1,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
