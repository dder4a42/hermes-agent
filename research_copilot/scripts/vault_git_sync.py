"""No-agent cron entry point for guarded Research Wiki Git publication."""

from __future__ import annotations

from research_copilot.vault_sync import sync_vault


def main() -> int:
    success, output = sync_vault()
    if output:
        print(output)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
