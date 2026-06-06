"""Tests for the embedding service's ``GET /broker/state`` endpoint.

The endpoint snapshots the process-global ``LocalInferenceBroker`` for the
dashboard's Inference sub-view. It enriches the recent-calls rows with a
relative ``age_s`` and computes the latency percentile window SERVER-SIDE (all
``SlotMetrics`` timestamps are ``time.monotonic()``), over the FULL ring filtered
to successful, finished-in-window calls — not the 50-row table slice.

We monkeypatch ``work_buddy.inference.get_broker`` (the name the handler imports
function-locally) to a broker we seed with controlled metrics, so timestamps and
statuses are deterministic.
"""
from __future__ import annotations

import time

import pytest

from work_buddy.embedding.service import app
from work_buddy.inference.broker import (
    LocalInferenceBroker,
    Priority,
    ProfileConfig,
    SlotMetrics,
)


@pytest.fixture
def client():
    return app.test_client()


@pytest.fixture(autouse=True)
def _empty_persisted(monkeypatch):
    """Default: no persisted history, so the live-ring tests stay deterministic.

    The endpoint merges the live ring with ``metrics_store.read_recent()``;
    stubbing it to ``[]`` isolates the in-memory behavior. Merge/restart tests
    override it explicitly.
    """
    monkeypatch.setattr(
        "work_buddy.inference.metrics_store.read_recent", lambda limit=200: []
    )


def _ok_row(
    profile: str, latency_s: float, finished_ago_s: float, now: float, rid: str = "ok",
) -> SlotMetrics:
    """A finished, successful call with a controlled total latency + recency.

    ``rid`` must be unique per row: the endpoint merges live+persisted and dedups
    by call id (real ids are unique uuids), so duplicate ids would collapse.
    """
    finished = now - finished_ago_s
    queued = finished - latency_s
    return SlotMetrics(
        id=rid,
        profile=profile,
        priority=Priority.WORKFLOW,
        queued_at=queued,
        admitted_at=queued,
        started_http_at=queued,
        finished_at=finished,
        status="ok",
    )


def test_empty_broker(client, monkeypatch):
    monkeypatch.setattr("work_buddy.inference.get_broker", lambda: LocalInferenceBroker())
    resp = client.get("/broker/state")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["profiles"] == {}
    assert data["recent"] == []
    assert data["latency"]["n"] == 0
    assert data["latency"]["p50"] is None
    assert data["latency"]["p95"] is None
    assert data["latency"]["p99"] is None
    assert data["latency"]["window_s"] == 300.0


def test_shape_window_and_null_handling(client, monkeypatch):
    b = LocalInferenceBroker()
    b.configure_profile(ProfileConfig(name="p1", max_concurrent=2, max_queued=16))
    now = time.monotonic()

    # 3 successful, in-window calls: 100 / 200 / 300 ms (unique ids).
    for i, lat in enumerate((0.1, 0.2, 0.3)):
        b._record(_ok_row("p1", lat, finished_ago_s=1.0, now=now, rid=f"ok{i}"))
    # A successful call that finished 400s ago: present in the table, but OUTSIDE
    # the 5-min latency window (forces full-ring-scan vs 50-slice correctness).
    b._record(_ok_row("p1", 0.999, finished_ago_s=400.0, now=now, rid="ok_old"))
    # An in-flight call: no finished_at → null latency splits, but a real age.
    b._record(SlotMetrics(
        id="run", profile="p1", priority=Priority.INTERACTIVE,
        queued_at=now - 2.0, admitted_at=now - 2.0, status="running",
    ))
    # A terminal error: excluded from the percentile set.
    b._record(SlotMetrics(
        id="err", profile="p1", priority=Priority.BACKGROUND,
        queued_at=now - 5.0, admitted_at=now - 5.0, finished_at=now - 4.0,
        status="error", error_kind="ValueError", error_detail="boom",
    ))

    monkeypatch.setattr("work_buddy.inference.get_broker", lambda: b)
    data = client.get("/broker/state").get_json()

    # Profiles: occupancy-card contract.
    p1 = data["profiles"]["p1"]
    assert p1["max_concurrent"] == 2
    assert set(p1["waiting"]) == {"INTERACTIVE", "WORKFLOW", "BACKGROUND"}

    # Recent: all 6 seeded rows, newest-first ordering handled client-side; each
    # row carries age_s. The in-flight row has null latency splits but a real age.
    assert len(data["recent"]) == 6
    assert all("age_s" in r for r in data["recent"])
    running = next(r for r in data["recent"] if r["status"] == "running")
    assert running["total_latency_ms"] is None
    assert running["service_time_ms"] is None
    assert running["age_s"] is not None and running["age_s"] >= 0

    # Latency window: only the 3 in-window ok rows count.
    lat = data["latency"]
    assert lat["n"] == 3
    assert lat["p50"] == 200.0  # nearest-rank over [100, 200, 300]
    assert lat["p95"] == 300.0
    assert lat["p99"] == 300.0


def test_single_ok_row_percentiles(client, monkeypatch):
    """n==1 must not blow up (nearest-rank handles it; statistics.quantiles wouldn't)."""
    b = LocalInferenceBroker()
    now = time.monotonic()
    b._record(_ok_row("p1", 0.15, finished_ago_s=1.0, now=now))
    monkeypatch.setattr("work_buddy.inference.get_broker", lambda: b)
    lat = client.get("/broker/state").get_json()["latency"]
    assert lat["n"] == 1
    assert lat["p50"] == 150.0 and lat["p95"] == 150.0 and lat["p99"] == 150.0


def _persisted(rid: str, *, status: str, finished_ago_s: float, latency_ms: float, wall: float) -> dict:
    """A persisted-store row (wall-clock), as ``metrics_store.read_recent`` returns."""
    finished = wall - finished_ago_s
    return {
        "id": rid, "profile": "lmstudio:m", "priority": "BACKGROUND", "status": status,
        "error_kind": None, "error_detail": None,
        "queued_at_wall": finished - latency_ms / 1000.0, "finished_at_wall": finished,
        "queue_wait_ms": 0.0, "service_time_ms": latency_ms, "total_latency_ms": latency_ms,
        "completed_at": "",
    }


def test_persisted_survives_restart(client, monkeypatch):
    """Empty live ring + populated persisted store → recent/latency from history.

    This is the "survives a sidecar reset" proof: the broker singleton is fresh
    (ring wiped), but the panel still shows prior calls from the persisted store.
    """
    monkeypatch.setattr("work_buddy.inference.get_broker", lambda: LocalInferenceBroker())
    wall = time.time()
    persisted = [
        _persisted("h1", status="ok", finished_ago_s=10.0, latency_ms=150.0, wall=wall),
        _persisted("h2", status="ok", finished_ago_s=5.0, latency_ms=250.0, wall=wall),
    ]
    monkeypatch.setattr(
        "work_buddy.inference.metrics_store.read_recent", lambda limit=200: persisted
    )
    data = client.get("/broker/state").get_json()
    assert {r["id"] for r in data["recent"]} == {"h1", "h2"}
    assert all(r["age_s"] is not None for r in data["recent"])
    assert data["latency"]["n"] == 2
    assert data["latency"]["p50"] == 150.0  # nearest-rank over [150, 250]
    assert data["latency"]["p95"] == 250.0


def test_merge_live_wins(client, monkeypatch):
    """Same id present live (running) + persisted (ok) → live wins (freshest)."""
    b = LocalInferenceBroker()
    now = time.monotonic()
    b._record(SlotMetrics(
        id="x", profile="lmstudio:m", priority=Priority.INTERACTIVE,
        queued_at=now - 1.0, admitted_at=now - 1.0, status="running",
    ))
    monkeypatch.setattr("work_buddy.inference.get_broker", lambda: b)
    monkeypatch.setattr(
        "work_buddy.inference.metrics_store.read_recent",
        lambda limit=200: [_persisted("x", status="ok", finished_ago_s=0.8,
                                       latency_ms=200.0, wall=time.time())],
    )
    data = client.get("/broker/state").get_json()
    rows = [r for r in data["recent"] if r["id"] == "x"]
    assert len(rows) == 1  # deduped by id
    assert rows[0]["status"] == "running"  # live wins over persisted
    assert data["latency"]["n"] == 0  # the live row is in-flight, not ok


def test_recent_ordering_oldest_first(client, monkeypatch):
    """`recent` is returned oldest-first; the frontend `.reverse()` makes it newest-first."""
    b = LocalInferenceBroker()
    now = time.monotonic()
    b._record(_ok_row("p1", 0.1, finished_ago_s=2.9, now=now, rid="old"))
    b._record(_ok_row("p1", 0.1, finished_ago_s=1.9, now=now, rid="mid"))
    b._record(_ok_row("p1", 0.1, finished_ago_s=0.9, now=now, rid="new"))
    monkeypatch.setattr("work_buddy.inference.get_broker", lambda: b)
    data = client.get("/broker/state").get_json()
    assert [r["id"] for r in data["recent"]] == ["old", "mid", "new"]
