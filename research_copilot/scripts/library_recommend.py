"""Cron shim for repository-owned Research Library recommendation."""

from __future__ import annotations

import argparse


def main() -> int:
    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    return cmd_research_copilot(argparse.Namespace(
        research_copilot_command="recommend", dry_run=False, threshold=0.72,
        delivery_context=True,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
