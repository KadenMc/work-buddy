"""Unit test for the notify-demo consumer."""

from __future__ import annotations

import work_buddy.notifications.dispatcher as ndisp
import work_buddy.notifications.store as nstore
from work_buddy.events.consumers.notify_demo import NotifyDemoProcessor
from work_buddy.events.envelope import new_event
from work_buddy.events.protocol import RunContext


def _fake_dispatcher(result, sink=None):
    class _Fake:
        def deliver(self, notif, mark_delivered_fn=None):
            if sink is not None:
                sink.append(notif)
            return result

    return classmethod(lambda cls: _Fake())


def test_notify_demo_creates_and_dispatches(monkeypatch):
    captured = []
    delivered_to = []
    monkeypatch.setattr(
        nstore, "create_notification", lambda n: captured.append(n) or n
    )
    monkeypatch.setattr(
        ndisp.SurfaceDispatcher,
        "from_config",
        _fake_dispatcher({"telegram": True, "dashboard": True}, sink=delivered_to),
    )

    proc = NotifyDemoProcessor()
    res = proc.run(
        new_event("/wb/agent", "ai.workbuddy.demo.ping", {"message": "hello spine"}),
        RunContext(seq=3),
    )

    assert res.text == "notified"
    assert res.is_error is False
    assert len(captured) == 1
    note = captured[0]
    assert note.title == "Events backbone"
    assert "hello spine" in note.body
    assert "events" in note.tags
    assert len(delivered_to) == 1                       # dispatched to surfaces
    assert "telegram" in res.structured["delivered"]


def test_notify_demo_delivery_failure_is_non_fatal(monkeypatch):
    monkeypatch.setattr(nstore, "create_notification", lambda n: n)

    def boom(cls):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(
        ndisp.SurfaceDispatcher, "from_config", classmethod(boom)
    )

    proc = NotifyDemoProcessor()
    res = proc.run(
        new_event("/wb/agent", "ai.workbuddy.demo.ping", {}), RunContext(seq=1)
    )

    # Notification is persisted regardless; a delivery failure must not raise
    # (which would retry → DLQ). The consumer still reports success.
    assert res.text == "notified"
    assert res.structured["delivered"] == []
