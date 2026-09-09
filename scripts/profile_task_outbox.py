"""Compare per-event and collection task invalidation on disposable data.

``uv run python scripts/profile_task_outbox.py`` creates its own synthetic
6,000-event SQLite backlog. It never opens the configured task authority.
The delivery callback counts notifications; no browser or network is used.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with ExitStack() as cleanup:
        root = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix="wb-outbox-profile-")))
        cleanup.callback(logging.shutdown)
        os.environ["WORK_BUDDY_DATA_DIR"] = str(root / "data")
        os.environ["WORK_BUDDY_CONFIG_DIR"] = str(root / "config")
        os.environ["WORK_BUDDY_SESSION_ID"] = "task-outbox-profile"
        from work_buddy.tasks.events import invalidation_payload, publish_pending_coalesced
        from work_buddy.tasks.store import TaskStore

        store = TaskStore(root / "tasks.db")
        store.initialize()
        count = 6000
        with store.transaction() as conn:
            conn.execute("INSERT INTO task_metadata(task_id,description,created_at,updated_at) "
                         "VALUES('fixture','Synthetic task','2026-09-09','2026-09-09')")
            conn.executemany(
                "INSERT INTO task_event_outbox(event_id,task_id,mutation,task_revision,"
                "collection_revision,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                [(f"event-{revision}", "fixture", "update", revision, revision,
                  json.dumps(invalidation_payload(task_id="fixture", mutation="update",
                                                  collection_revision=revision)), "2026-09-09")
                 for revision in range(1, count + 1)],
            )
        delivered = []
        started = time.perf_counter()
        # Reproduce the previous API's bounded, per-event publish/ack loop.
        for event in store.pending_outbox(limit=100):
            delivered.append(("task.changed", event["payload"]))
            store.mark_outbox_published(
                event["event_id"], published_at=datetime.now(timezone.utc).isoformat()
            )
        legacy_ms = (time.perf_counter() - started) * 1000
        legacy = {"duration_ms": round(legacy_ms, 2), "notifications": len(delivered),
                  "remaining_events": count - len(delivered), "acknowledgement_transactions": 100}
        with store.transaction() as conn:
            conn.execute("UPDATE task_event_outbox SET published_at=NULL,attempts=0,last_error=NULL")
        delivered = []
        started = time.perf_counter()
        result = publish_pending_coalesced(
            store, delivery=lambda name, payload: delivered.append((name, payload)) or True
        )
        coalesced_ms = (time.perf_counter() - started) * 1000
        assert result["published"] == count
        assert store.pending_outbox_invalidation() is None
        print(json.dumps({
            "environment": "disposable synthetic SQLite; counting delivery callback; no HTTP/browser",
            "backlog_events": count,
            "previous_bounded_drain": legacy,
            "collection_drain": {"duration_ms": round(coalesced_ms, 2), "notifications": len(delivered),
                                 "remaining_events": 0, "acknowledgement_transactions": 1},
            "speedup": round(legacy_ms / coalesced_ms, 2),
        }, indent=2))


if __name__ == "__main__":
    main()
