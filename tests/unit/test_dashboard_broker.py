"""Tests for the dashboard's broker data layer + the broker.state SSE event.

Data layer mirrors the embeddings-snapshot pattern (background-refreshed cache),
with one difference: the build does a cross-process HTTP call, so a cold miss
fast-fails to ``{available: False}`` instead of blocking. The poller publishes a
thin ``broker.state`` ping; the SSE stream must deliver it.
"""
from __future__ import annotations

import json
import threading
import time

from work_buddy.dashboard import api
from work_buddy.dashboard.events import EventBus
from work_buddy.dashboard.service import _sse_stream


def setup_function():
    api._broker_cache = None
    api._broker_cache_ts = 0.0
    api._broker_refreshing = False


def test_build_wraps_available(monkeypatch):
    state = {"profiles": {}, "recent": [], "latency": {"n": 0}, "now_monotonic": 1.0}
    monkeypatch.setattr("work_buddy.embedding.client.broker_state", lambda: state)
    out = api._build_broker_summary()
    assert out["available"] is True
    assert out["recent"] == []
    assert out["latency"] == {"n": 0}


def test_build_unavailable(monkeypatch):
    monkeypatch.setattr("work_buddy.embedding.client.broker_state", lambda: None)
    assert api._build_broker_summary() == {"available": False}


def test_refresh_then_get_serves_cache(monkeypatch):
    calls = {"n": 0}

    def _bs():
        calls["n"] += 1
        return {"profiles": {}, "recent": [{"id": "a"}], "latency": {"n": 0}}

    monkeypatch.setattr("work_buddy.embedding.client.broker_state", _bs)
    fresh = api.refresh_broker_cache()
    assert fresh["available"] is True
    again = api.get_broker_summary()
    assert again == fresh
    assert calls["n"] == 1  # second read served from the cache, no new client call


def test_cold_get_fast_fails(monkeypatch):
    # No cache: return the immediate "no data yet" envelope WITHOUT a blocking
    # synchronous build. (Kick is stubbed so no background thread is spawned.)
    monkeypatch.setattr(api, "_kick_broker_refresh", lambda: None)
    assert api.get_broker_summary() == {"available": False}


def test_broker_state_event_delivered_over_sse():
    bus = EventBus()
    gen = _sse_stream(bus, idle_timeout=0.05)
    next(gen)  # ": connected" — registers the subscriber

    def _pub():
        time.sleep(0.02)
        bus.publish("broker.state", {"available": True, "in_flight_total": 1, "n_recent": 3})

    threading.Thread(target=_pub, daemon=True).start()

    deadline = time.time() + 1.0
    seen = None
    while time.time() < deadline:
        chunk = next(gen)
        if chunk.startswith(b"data: "):
            seen = chunk
            break
    assert seen is not None, "no broker.state data frame arrived"
    parsed = json.loads(seen[len(b"data: "):-2].decode("utf-8"))
    assert parsed["event_type"] == "broker.state"
    assert parsed["payload"]["in_flight_total"] == 1
    assert parsed["payload"]["n_recent"] == 3
    gen.close()
