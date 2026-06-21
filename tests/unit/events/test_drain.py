"""Unit test for the event-drain thread."""

from __future__ import annotations

import time

from work_buddy.events import dispatcher
from work_buddy.events.drain import EventDrain


def test_drain_thread_ticks_then_stops(monkeypatch):
    calls = {"n": 0}

    def fake_drain():
        calls["n"] += 1
        return {"delivered": 0, "dlq": 0}

    monkeypatch.setattr(dispatcher, "drain", fake_drain)

    d = EventDrain(interval_s=0.05)
    d.start()
    time.sleep(0.22)
    d.stop()

    assert calls["n"] >= 1  # the loop ticked
    settled = calls["n"]
    time.sleep(0.15)
    assert calls["n"] == settled  # and stopped cleanly (no more ticks)
