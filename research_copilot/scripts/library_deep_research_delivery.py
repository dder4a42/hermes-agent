"""Retry pending deep-research cards without starting research work."""

from __future__ import annotations

from datetime import datetime, timezone


def main() -> int:
    from research_copilot.runtime import open_library, runtime_paths
    from research_copilot.scripts.library_deep_research import reconcile_previous_delivery

    reconcile_previous_delivery()
    connection, repository = open_library(runtime_paths()["database"])
    try:
        pending = repository.pending_deep_research_delivery()
        if pending is None:
            return 0
        repository.mark_deep_research_delivery_attempt(
            pending["id"], attempted_at=datetime.now(timezone.utc),
        )
        payload = str(pending["payload_text"])
    finally:
        connection.close()
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
