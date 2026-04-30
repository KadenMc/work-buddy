"""Unit tests for the dashboard SSE endpoint and its stream helper.

The route delegates the streaming generator to ``_sse_stream``, which
takes a bus and an idle timeout and yields SSE-framed bytes. We test
the helper directly (no Flask server) for the protocol shape, then
exercise the route via the Flask test client to confirm wiring.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from work_buddy.dashboard.events import EventBus
from work_buddy.dashboard.service import _format_sse_event, _sse_stream, app


def test_format_sse_event_shape():
    raw = _format_sse_event({"event_type": "x", "payload": 1, "ts": 0.5})
    assert raw.startswith(b"data: ")
    assert raw.endswith(b"\n\n")
    body = raw[len(b"data: "):-2].decode("utf-8")
    assert json.loads(body) == {"event_type": "x", "payload": 1, "ts": 0.5}


def test_sse_stream_first_chunk_is_connected_comment():
    bus = EventBus()
    gen = _sse_stream(bus, idle_timeout=0.05)
    first = next(gen)
    assert first == b": connected\n\n"
    gen.close()


def test_sse_stream_idle_emits_keepalive():
    bus = EventBus()
    gen = _sse_stream(bus, idle_timeout=0.05)
    next(gen)  # connected
    second = next(gen)
    assert second == b": keepalive\n\n"
    gen.close()


def test_sse_stream_delivers_published_event():
    bus = EventBus()
    gen = _sse_stream(bus, idle_timeout=0.05)
    next(gen)  # connected — registers subscriber

    # Publish from a thread so the generator's blocking pop wakes up.
    def _pub():
        time.sleep(0.02)
        bus.publish("pool.entry_added", {"item_id": "abc"})

    threading.Thread(target=_pub, daemon=True).start()

    # Drain until we see the data frame (skipping any keepalives).
    deadline = time.time() + 1.0
    seen = None
    while time.time() < deadline:
        chunk = next(gen)
        if chunk.startswith(b"data: "):
            seen = chunk
            break
    assert seen is not None, "no data frame arrived"
    body = seen[len(b"data: "):-2].decode("utf-8")
    parsed = json.loads(body)
    assert parsed["event_type"] == "pool.entry_added"
    assert parsed["payload"] == {"item_id": "abc"}
    assert isinstance(parsed["ts"], float)
    gen.close()


def test_sse_stream_close_unsubscribes():
    bus = EventBus()
    gen = _sse_stream(bus, idle_timeout=0.05)
    next(gen)  # ": connected" — does not yet enter the subscribe loop
    next(gen)  # first iteration of the for-loop: registers + yields keepalive
    assert bus.subscriber_count() == 1
    gen.close()
    # Generator close runs through subscribe()'s try/finally.
    assert bus.subscriber_count() == 0


def test_route_sets_sse_headers_and_streams():
    """End-to-end wiring through Flask's test client.

    Verifies the response is a stream and the headers are set, but does
    not consume the body indefinitely (``response.close`` releases the
    generator, which unsubscribes).
    """
    client = app.test_client()
    resp = client.get("/api/events", buffered=False)
    try:
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        assert resp.headers.get("Cache-Control") == "no-cache"
        # Read just enough to confirm a frame arrives.
        first_chunk = next(resp.response)
        assert b": connected" in first_chunk
    finally:
        resp.close()
