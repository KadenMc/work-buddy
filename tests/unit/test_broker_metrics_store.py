"""Tests for the persistent broker-metrics store + its entry-TTL artifact.

The store persists completed ``LocalInferenceBroker`` calls so the dashboard
Inference panel survives restarts. Covers: terminal-only persistence, idempotent
re-flush, monotonic→wall conversion + ISO ``completed_at`` (the TTL contract),
newest-first reads, and end-to-end TTL pruning via the artifact registry.
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from work_buddy.inference import metrics_store


@pytest.fixture(autouse=True)
def _tmp_db(monkeypatch, tmp_path):
    """Point the store at a throwaway DB and force a fresh schema per test."""
    db = tmp_path / "broker_metrics.db"
    monkeypatch.setattr(metrics_store, "_db_path", lambda: db)
    metrics_store._schema_ready.clear()
    yield db
    metrics_store._schema_ready.clear()


def _row(rid: str, *, status: str = "ok", finished: float | None = 999.0,
         queued: float = 998.0) -> dict:
    """A broker.snapshot_metrics()-shaped row (monotonic timestamps)."""
    return {
        "id": rid, "profile": "lmstudio:m", "priority": "BACKGROUND", "status": status,
        "error_kind": None, "error_detail": None,
        "queued_at": queued, "finished_at": finished,
        "queue_wait_ms": 0.0, "service_time_ms": 200.0, "total_latency_ms": 200.0,
    }


def test_only_terminal_rows_persist():
    rows = [
        _row("ok1", status="ok"),
        _row("err", status="error"),
        _row("qf", status="queue_full"),
        _row("qwt", status="queue_wait_timeout"),
        _row("it", status="inference_timeout"),
        _row("running", status="running", finished=None),
        _row("queued", status="queued", finished=None),
    ]
    written = metrics_store.persist_terminal_rows(rows, mono_now=1000.0, wall_now=1_000_000.0)
    assert written == 5
    ids = {r["id"] for r in metrics_store.read_recent()}
    assert ids == {"ok1", "err", "qf", "qwt", "it"}


def test_insert_or_ignore_dedup():
    r = _row("dup")
    assert metrics_store.persist_terminal_rows([r], 1000.0, 1_000_000.0) == 1
    # Re-flushing the same (immutable) terminal row is a no-op.
    assert metrics_store.persist_terminal_rows([r], 1000.0, 1_000_000.0) == 0
    assert len([x for x in metrics_store.read_recent() if x["id"] == "dup"]) == 1


def test_monotonic_to_wall_and_iso_completed_at():
    mono, wall = 5000.0, 2_000_000.0
    # finished 30s before mono_now, queued 30.2s before.
    metrics_store.persist_terminal_rows(
        [_row("t", finished=mono - 30.0, queued=mono - 30.2)], mono, wall
    )
    row = metrics_store.read_recent()[0]
    assert abs(row["finished_at_wall"] - (wall - 30.0)) < 1e-6
    assert abs(row["queued_at_wall"] - (wall - 30.2)) < 1e-6
    # completed_at must be ISO-parseable — PerRecordTtl reads it via fromisoformat;
    # an epoch string would silently never expire.
    parsed = datetime.fromisoformat(row["completed_at"])
    assert abs(parsed.timestamp() - (wall - 30.0)) < 1.0


def test_read_recent_is_newest_first():
    metrics_store.persist_terminal_rows([
        _row("a", finished=100.0, queued=99.0),
        _row("b", finished=200.0, queued=199.0),
        _row("c", finished=300.0, queued=299.0),
    ], mono_now=1000.0, wall_now=1_000_000.0)
    assert [r["id"] for r in metrics_store.read_recent()] == ["c", "b", "a"]


def test_ttl_prune_drops_old_keeps_recent():
    mono, wall = 5000.0, time.time()
    metrics_store.persist_terminal_rows([
        _row("old", finished=mono - 8 * 86400, queued=mono - 8 * 86400 - 0.2),
        _row("fresh", finished=mono - 1 * 86400, queued=mono - 1 * 86400 - 0.2),
    ], mono, wall)
    # Re-register so the artifact's SqliteRowsStorage points at the tmp DB.
    metrics_store._register_broker_metrics_artifact()
    from work_buddy.artifacts.registry import sweep_all

    sweep_all(dry_run=False, name="broker-metrics")
    ids = {r["id"] for r in metrics_store.read_recent()}
    assert "fresh" in ids
    assert "old" not in ids  # 8 days > 7-day TTL


def test_artifact_is_registered():
    from work_buddy.artifacts import list_artifact_names

    metrics_store._register_broker_metrics_artifact()
    assert "broker-metrics" in list_artifact_names()
