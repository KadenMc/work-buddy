"""Unit tests for the events artifact retention predicate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from work_buddy.events import store as evstore
from work_buddy.events.artifact import _make_events_keep, _older_than_days
from work_buddy.events.store import EventStore, _expires_at_for, _now_iso


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(evstore, "_db_path", lambda: tmp_path / "events.db")
    return evstore.EventStore()


def test_keep_undelivered(store):
    store.ensure_offset("c1", 5)  # min_live_offset = 5
    keep = _make_events_keep(store)
    assert keep({"seq": 6, "modality": "internal", "received_at": _now_iso()}) is True


def test_reap_delivered_internal(store):
    store.ensure_offset("c1", 5)
    keep = _make_events_keep(store)
    rec = {"seq": 3, "modality": "internal", "received_at": _iso_days_ago(10)}
    assert keep(rec) is False


def test_keep_external_within_replay_window(store):
    store.ensure_offset("c1", 5)
    keep = _make_events_keep(store)
    assert keep({"seq": 3, "modality": "push", "received_at": _now_iso()}) is True


def test_reap_external_past_replay_window(store):
    store.ensure_offset("c1", 5)
    keep = _make_events_keep(store)
    assert keep({"seq": 3, "modality": "push", "received_at": _iso_days_ago(10)}) is False


def test_keep_dlq_row(store):
    store.ensure_offset("c1", 5)
    store.dead_letter(3, "c1", 3, "boom")
    keep = _make_events_keep(store)
    rec = {"seq": 3, "modality": "internal", "received_at": _iso_days_ago(10)}
    assert keep(rec) is True  # DLQ rows are pinned until triaged


def test_older_than_days_helper():
    assert _older_than_days(_iso_days_ago(10), 7.0) is True
    assert _older_than_days(_now_iso(), 7.0) is False
    assert _older_than_days(None, 7.0) is True  # missing → not pinned


def test_expires_at_per_type_ordering():
    now = _now_iso()
    tick = _expires_at_for("ai.workbuddy.schedule.tick", now)  # ~3h
    default = _expires_at_for("ai.workbuddy.demo.ping", now)   # 14d
    assert tick < default  # noisy type self-reaps sooner (ISO sorts lexically)


def test_artifact_composition_is_coherent(tmp_path):
    # PerRecordTtl + SqliteRowsStorage must not raise IncoherentComposition.
    from work_buddy.artifacts import (
        Artifact,
        Delete,
        Lifecycle,
        PerRecordTtl,
        SqliteRowsStorage,
    )

    art = Artifact(
        name="events-test",
        storage=SqliteRowsStorage(
            db_path=tmp_path / "events.db", table="events", id_column="seq"
        ),
        lifecycle=Lifecycle(
            trigger=PerRecordTtl(ttl_field="expires_at"),
            action=Delete(),
            retention_predicate=_make_events_keep(EventStore()),
        ),
    )
    assert art.lifecycle.retention_predicate is not None
    assert "events" in art.name
