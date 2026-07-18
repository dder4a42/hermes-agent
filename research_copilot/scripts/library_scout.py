"""Cron shim for low-frequency Codex Research Scout discovery."""

from __future__ import annotations

import argparse


def main() -> int:
    from hermes_cli.research_copilot_cmd import cmd_research_copilot

    return cmd_research_copilot(argparse.Namespace(
        research_copilot_command="scout", dry_run=False, timeout=2700,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
