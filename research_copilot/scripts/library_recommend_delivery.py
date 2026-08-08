"""Retry a pending recommendation without selecting or consuming a new one."""

from __future__ import annotations

import json
from datetime import datetime, timezone


def main() -> int:
    from research_copilot.runtime import open_library, runtime_paths
    from research_copilot.scripts.library_recommend import reconcile_previous_delivery

    reconcile_previous_delivery()
    connection, repository = open_library(runtime_paths()["database"])
    try:
        pending = repository.pending_delivery()
        if pending is None or not pending.get("payload"):
            return 0
        repository.mark_delivery_attempt(
            pending["id"], attempted_at=datetime.now(timezone.utc),
        )
        payload = pending["payload"]
    finally:
        connection.close()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
