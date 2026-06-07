"""Tests for the dashboard inference-activity read-model + route.

Cross-session scan of the per-session inference_calls.jsonl logs, newest-first,
with the broker-latency join by call_id, served from a short-TTL cache.
"""
from __future__ import annotations

import json
import pathlib
import tempfile

from work_buddy.dashboard import api


def setup_function():
    api._inference_cache = None
    api._inference_cache_ts = 0.0
    api._inference_refreshing = False


def _seed(monkeypatch, sessions: dict[str, list[dict]]) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    for sid, rows in sessions.items():
        d = root / sid
        d.mkdir()
        (d / "inference_calls.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
    monkeypatch.setattr(api, "_AGENTS_DIR", root)
    monkeypatch.setattr(
        "work_buddy.inference.metrics_store.read_recent", lambda limit=1000: []
    )
    return root


def test_build_sorts_newest_first_and_counts(monkeypatch):
    _seed(monkeypatch, {
        "sessA": [{"call_id": "a", "finished_at": "2026-06-06T10:00:00+00:00", "kind": "completion"}],
        "sessB": [{"call_id": "b", "finished_at": "2026-06-06T12:00:00+00:00", "kind": "embedding"}],
    })
    out = api._build_inference_activity()
    assert out["count"] == 2
    assert [c["call_id"] for c in out["calls"]] == ["b", "a"]  # newest first
    assert out["calls"][0]["session"] == "sessB"[:8]


def test_build_tolerates_corrupt_lines(monkeypatch):
    root = pathlib.Path(tempfile.mkdtemp())
    d = root / "s"
    d.mkdir()
    (d / "inference_calls.jsonl").write_text(
        '{"call_id":"ok","finished_at":"2026-06-06T10:00:00+00:00"}\nNOT JSON\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "_AGENTS_DIR", root)
    monkeypatch.setattr(
        "work_buddy.inference.metrics_store.read_recent", lambda limit=1000: []
    )
    out = api._build_inference_activity()
    assert out["count"] == 1


def test_build_joins_broker_latency_by_call_id(monkeypatch):
    _seed(monkeypatch, {
        "s": [{"call_id": "shared", "finished_at": "2026-06-06T10:00:00+00:00",
               "kind": "completion", "latency_ms": None}],
    })
    monkeypatch.setattr(
        "work_buddy.inference.metrics_store.read_recent",
        lambda limit=1000: [{
            "id": "shared", "queue_wait_ms": 5.0,
            "service_time_ms": 50.0, "total_latency_ms": 55.0,
        }],
    )
    r = api._build_inference_activity()["calls"][0]
    assert r["queue_wait_ms"] == 5.0 and r["service_time_ms"] == 50.0
    assert r["latency_ms"] == 55.0  # filled from broker when the row had None


def test_get_inference_activity_caches(monkeypatch):
    calls = {"n": 0}

    def _fake_build():
        calls["n"] += 1
        return {"calls": [], "count": 0}

    monkeypatch.setattr(api, "_build_inference_activity", _fake_build)
    a = api.get_inference_activity()
    b = api.get_inference_activity()
    assert calls["n"] == 1 and a == b  # second read served from cache


def test_route_shape(monkeypatch):
    from work_buddy.dashboard.service import app
    monkeypatch.setattr(
        api, "_build_inference_activity",
        lambda: {"calls": [{"call_id": "z"}], "count": 1},
    )
    resp = app.test_client().get("/api/inference-activity")
    assert resp.status_code == 200
    assert resp.get_json()["count"] == 1
