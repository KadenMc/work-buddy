"""Unit tests for the dashboard cross-process messaging bridge.

Exercises ``_drain_once`` against monkeypatched messaging-client
functions, plus a smoke test for ``publish_cross_process``.
"""

from __future__ import annotations

import json
import time

import pytest

from work_buddy.dashboard import messaging_bridge as bridge
from work_buddy.dashboard.events import EventBus, publish_cross_process


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMessaging:
    """Minimal in-memory stand-in for messaging.client functions."""

    def __init__(self):
        self.queue: list[dict] = []
        self.bodies: dict[str, str] = {}
        self.statuses: dict[str, str] = {}

    def add(self, msg_id: str, msg_type: str, body: str | None):
        self.queue.append({"id": msg_id, "type": msg_type})
        if body is not None:
            self.bodies[msg_id] = body
        self.statuses[msg_id] = "unread"

    def query_messages(self, *, recipient, status, limit):
        # Return only currently-unread, in fifo order.
        return [
            m for m in self.queue
            if self.statuses.get(m["id"]) == status
        ][:limit]

    def read_message(self, msg_id):
        if msg_id not in self.bodies and msg_id not in self.statuses:
            return None
        return {"id": msg_id, "body": self.bodies.get(msg_id)}

    def update_status(self, msg_id, new_status):
        self.statuses[msg_id] = new_status
        return {"id": msg_id, "status": new_status}


@pytest.fixture
def fake_messaging(monkeypatch):
    fake = _FakeMessaging()
    monkeypatch.setattr(
        "work_buddy.messaging.client.query_messages",
        lambda **kw: fake.query_messages(**kw),
    )
    monkeypatch.setattr(
        "work_buddy.messaging.client.read_message",
        lambda msg_id, *a, **kw: fake.read_message(msg_id),
    )
    monkeypatch.setattr(
        "work_buddy.messaging.client.update_status",
        lambda msg_id, new_status: fake.update_status(msg_id, new_status),
    )
    return fake


# ---------------------------------------------------------------------------
# _drain_once
# ---------------------------------------------------------------------------


def test_drain_publishes_each_bus_event(fake_messaging):
    bus = EventBus()
    sub = bus.subscribe(timeout=0.05)
    next(sub)  # register

    fake_messaging.add(
        "m1",
        "bus.event",
        json.dumps({"event_type": "pool.entry_added", "payload": {"item_id": "abc"}}),
    )
    fake_messaging.add(
        "m2",
        "bus.event",
        json.dumps({"event_type": "task.created", "payload": {"task_id": "t-1"}}),
    )

    delivered = bridge._drain_once(bus)
    assert delivered == 2

    # Drain the bus
    seen = []
    for _ in range(2):
        evt = next(sub)
        if evt is not None:
            seen.append(evt)

    types = {e["event_type"] for e in seen}
    assert types == {"pool.entry_added", "task.created"}
    assert fake_messaging.statuses["m1"] == "resolved"
    assert fake_messaging.statuses["m2"] == "resolved"
    sub.close()


def test_drain_ignores_non_bus_messages(fake_messaging):
    bus = EventBus()
    fake_messaging.add(
        "m1",
        "agent_request",
        json.dumps({"event_type": "should.not.publish"}),
    )
    fake_messaging.add(
        "m2",
        "bus.event",
        json.dumps({"event_type": "pool.entry_added", "payload": {}}),
    )
    delivered = bridge._drain_once(bus)
    assert delivered == 1
    # Non-bus message untouched.
    assert fake_messaging.statuses["m1"] == "unread"
    assert fake_messaging.statuses["m2"] == "resolved"


def test_drain_skips_invalid_envelope(fake_messaging):
    bus = EventBus()
    fake_messaging.add("m1", "bus.event", "not json{")
    fake_messaging.add(
        "m2",
        "bus.event",
        json.dumps({"event_type": 123}),  # bad: not a string
    )
    fake_messaging.add(
        "m3",
        "bus.event",
        json.dumps({"event_type": "ok.event"}),
    )
    delivered = bridge._drain_once(bus)
    assert delivered == 1
    # Bad messages get marked resolved so we don't retry forever.
    assert fake_messaging.statuses["m1"] == "resolved"
    assert fake_messaging.statuses["m2"] == "resolved"
    assert fake_messaging.statuses["m3"] == "resolved"


def test_drain_does_not_mark_resolved_when_publish_raises(monkeypatch, fake_messaging):
    """Republish failures keep the message unread for next-cycle retry."""
    bus = EventBus()
    fake_messaging.add(
        "m1",
        "bus.event",
        json.dumps({"event_type": "pool.entry_added", "payload": {}}),
    )

    def boom(*a, **kw):
        raise RuntimeError("publish failed")

    monkeypatch.setattr(bus, "publish", boom)
    delivered = bridge._drain_once(bus)
    assert delivered == 0
    assert fake_messaging.statuses["m1"] == "unread"


def test_drain_handles_query_failure(monkeypatch):
    """If query_messages raises, _drain_once returns 0 — no crash."""
    monkeypatch.setattr(
        "work_buddy.messaging.client.query_messages",
        lambda **kw: (_ for _ in ()).throw(ConnectionError("messaging down")),
    )
    bus = EventBus()
    assert bridge._drain_once(bus) == 0


# ---------------------------------------------------------------------------
# start_messaging_bridge thread
# ---------------------------------------------------------------------------


def test_start_messaging_bridge_runs_drain_repeatedly(fake_messaging):
    bus = EventBus()
    sub = bus.subscribe(timeout=0.5)
    next(sub)

    stop = bridge.start_messaging_bridge(poll_interval=0.05, bus=bus)
    try:
        # Add a message after the bridge starts.
        time.sleep(0.05)
        fake_messaging.add(
            "m1",
            "bus.event",
            json.dumps({"event_type": "task.created", "payload": {"id": "t-1"}}),
        )
        # Drain a real event.
        deadline = time.time() + 2.0
        seen = None
        while time.time() < deadline:
            evt = next(sub)
            if evt is not None:
                seen = evt
                break
        assert seen is not None
        assert seen["event_type"] == "task.created"
    finally:
        stop.set()
        sub.close()


# ---------------------------------------------------------------------------
# publish_cross_process
# ---------------------------------------------------------------------------


def test_publish_cross_process_calls_send_message(monkeypatch):
    captured = {}

    def fake_send(**kwargs):
        captured.update(kwargs)
        return {"id": "m1"}

    monkeypatch.setattr("work_buddy.messaging.client.send_message", fake_send)

    ok = publish_cross_process("pool.entry_added", {"item_id": "abc"})
    assert ok is True
    assert captured["recipient"] == "dashboard"
    assert captured["type"] == "bus.event"
    assert captured["subject"] == "pool.entry_added"
    body = json.loads(captured["body"])
    assert body["event_type"] == "pool.entry_added"
    assert body["payload"] == {"item_id": "abc"}


def test_publish_cross_process_returns_false_when_send_fails(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.messaging.client.send_message",
        lambda **kw: None,
    )
    ok = publish_cross_process("any.event", {})
    assert ok is False


def test_publish_cross_process_swallows_exceptions(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.messaging.client.send_message",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    # Must not raise — primary work should not fail because of a
    # missed cross-process event.
    assert publish_cross_process("any.event", {}) is False
