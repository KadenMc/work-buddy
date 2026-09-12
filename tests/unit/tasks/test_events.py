from __future__ import annotations

import json
import sqlite3

from work_buddy.tasks.events import invalidation_payload, publish_pending, publish_pending_coalesced


class FakeOutbox:
    def __init__(self) -> None:
        self.events = [
            {"event_id": "ok", "payload": {"revision": 1}},
            {"event_id": "retry", "payload": {"revision": 2}},
        ]
        self.published: list[str] = []
        self.failed: list[str] = []

    def pending_outbox(self, *, limit: int = 100):
        return self.events[:limit]

    def mark_outbox_published(self, event_id: str, *, published_at: str) -> bool:
        assert published_at
        self.published.append(event_id)
        return True

    def record_outbox_failure(self, event_id: str, *, error: str) -> bool:
        assert error
        self.failed.append(event_id)
        return True


def test_outbox_marks_only_confirmed_dashboard_delivery() -> None:
    store = FakeOutbox()
    seen: list[tuple[str, dict]] = []

    def deliver(event_type: str, payload: dict) -> bool:
        seen.append((event_type, payload))
        return payload["revision"] == 1

    result = publish_pending(store, delivery=deliver)

    assert result == {"published": 1, "failed": 1}
    assert store.published == ["ok"]
    assert store.failed == ["retry"]
    assert seen == [
        ("task.changed", {"revision": 1}),
        ("task.changed", {"revision": 2}),
    ]


def seed_outbox(store, count):
    """Disposable backlog: ordinary task metadata remains unchanged by drains."""
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO task_metadata(task_id,description,created_at,updated_at) "
            "VALUES('backlog-task','Outbox fixture','2026-09-09','2026-09-09')"
        )
        conn.executemany(
            "INSERT INTO task_event_outbox(event_id,task_id,mutation,task_revision,"
            "collection_revision,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            [(f"event-{revision}", "backlog-task", "update", revision, revision,
              json.dumps(invalidation_payload(task_id="backlog-task", mutation="update",
                                              collection_revision=revision)), "2026-09-09")
             for revision in range(1, count + 1)],
        )
        conn.execute("UPDATE task_collection_state SET revision=?", (count,))


def test_thousands_of_pending_events_produce_one_collection_invalidation(task_store, monkeypatch):
    seed_outbox(task_store, 6000)
    seen = []
    connections = []
    original = task_store.connect

    def connect():
        connections.append(True)
        return original()

    monkeypatch.setattr(task_store, "connect", connect)
    result = publish_pending_coalesced(task_store, delivery=lambda name, payload: seen.append((name, payload)) or True)
    assert result == {"published": 6000, "failed": 0}
    assert len(connections) == 1  # acknowledgement, never a connection per row
    assert seen == [("task.collection_changed", {
        "app_id": "wb.tasks", "view_ids": ["wb.tasks.workspace"],
        "revision": 6000, "scope": "collection", "event_count": 6000,
    })]
    conn = task_store.connect_readonly()
    try:
        assert conn.execute("SELECT COUNT(*) FROM task_event_outbox WHERE published_at IS NULL").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_event_outbox WHERE attempts=1").fetchone()[0] == 6000
        assert conn.execute("SELECT revision FROM task_metadata").fetchone()[0] == 1
        assert conn.execute("SELECT revision FROM task_collection_state").fetchone()[0] == 6000
    finally:
        conn.close()
    assert publish_pending_coalesced(task_store, delivery=lambda *_: seen.append("unexpected")) == {"published": 0, "failed": 0}
    assert len(seen) == 1


def test_collection_acknowledgement_excludes_changes_committed_during_delivery(task_store):
    seed_outbox(task_store, 2)

    def deliver(_name, payload):
        assert payload["revision"] == 2
        with task_store.transaction() as conn:
            conn.execute(
                "INSERT INTO task_event_outbox(event_id,task_id,mutation,task_revision,"
                "collection_revision,payload_json,created_at) VALUES('later','backlog-task','update',3,3,'{}','2026-09-09')"
            )
            conn.execute("UPDATE task_collection_state SET revision=3")
        return True

    assert publish_pending_coalesced(task_store, delivery=deliver) == {"published": 2, "failed": 0}
    assert [event["event_id"] for event in task_store.pending_outbox()] == ["later"]
    seen = []
    assert publish_pending_coalesced(task_store, delivery=lambda _name, payload: seen.append(payload) or True) == {"published": 1, "failed": 0}
    assert seen[0]["revision"] == 3


def test_failed_collection_delivery_retains_every_event_then_retries(task_store):
    seed_outbox(task_store, 142)

    def failure(*_args):
        raise ConnectionError("Unavailable test bus")

    assert publish_pending_coalesced(task_store, delivery=failure) == {"published": 0, "failed": 142}
    pending = task_store.pending_outbox(limit=1000)
    assert len(pending) == 142
    assert all(event["attempts"] == 1 and "Unavailable test bus" in event["last_error"] for event in pending)
    assert publish_pending_coalesced(task_store, delivery=lambda *_: True) == {"published": 142, "failed": 0}
    conn = task_store.connect_readonly()
    try:
        assert conn.execute("SELECT COUNT(*) FROM task_event_outbox WHERE attempts=2 AND last_error IS NULL").fetchone()[0] == 142
    finally:
        conn.close()


def test_acknowledgement_failure_does_not_report_committed_mutation_failed(task_store, monkeypatch, caplog):
    seed_outbox(task_store, 2)
    original = task_store.record_outbox_invalidation_delivery

    def fail_ack(*_args, **_kwargs):
        raise sqlite3.OperationalError("Simulated acknowledgement failure")

    monkeypatch.setattr(task_store, "record_outbox_invalidation_delivery", fail_ack)
    seen = []
    result = publish_pending_coalesced(task_store, delivery=lambda name, payload: seen.append((name, payload)) or True)
    assert result == {"published": 0, "failed": 2}
    assert len(task_store.pending_outbox()) == 2
    assert "remains pending" in caplog.text
    monkeypatch.setattr(task_store, "record_outbox_invalidation_delivery", original)
    assert publish_pending_coalesced(task_store, delivery=lambda name, payload: seen.append((name, payload)) or True) == {"published": 2, "failed": 0}
    assert seen[0] == seen[1]  # safe at-least-once invalidation


def test_snapshot_failure_does_not_report_committed_mutation_failed(task_store, monkeypatch, caplog):
    seed_outbox(task_store, 2)
    original = task_store.pending_outbox_invalidation

    def fail_snapshot():
        raise sqlite3.OperationalError("Simulated snapshot read failure")

    monkeypatch.setattr(task_store, "pending_outbox_invalidation", fail_snapshot)
    seen = []
    result = publish_pending_coalesced(task_store, delivery=lambda name, payload: seen.append((name, payload)) or True)
    assert result == {"published": 0, "failed": None}
    assert not seen
    assert len(task_store.pending_outbox()) == 2
    assert "snapshot failure" in caplog.text
    monkeypatch.setattr(task_store, "pending_outbox_invalidation", original)
    assert publish_pending_coalesced(task_store, delivery=lambda name, payload: seen.append((name, payload)) or True) == {"published": 2, "failed": 0}
    assert len(seen) == 1


def test_exact_delivery_preserves_payloads_and_batches_bookkeeping(task_store, monkeypatch):
    seed_outbox(task_store, 100)
    connections = []
    original = task_store.connect

    def connect():
        connections.append(True)
        return original()

    monkeypatch.setattr(task_store, "connect", connect)
    seen = []
    result = publish_pending(task_store, delivery=lambda name, payload: seen.append((name, payload)) or payload["revision"] <= 50)
    assert result == {"published": 50, "failed": 50}
    assert len(connections) == 3  # one read, one success batch, one failure batch
    assert len(seen) == 100
    assert all(name == "task.changed" and payload["task_id"] == "backlog-task" for name, payload in seen)
    assert len(task_store.pending_outbox()) == 50
