"""Unit tests for the EventStore — durable log, dedup, offsets, DLQ."""

from __future__ import annotations

import sys

import pytest

from work_buddy.events import store as evstore
from work_buddy.events.envelope import new_event


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    db = tmp_path / "events.db"
    monkeypatch.setattr(evstore, "_db_path", lambda: db)
    return evstore.EventStore()


def test_append_returns_seq(fresh_store):
    seq = fresh_store.append(new_event("/wb/test", "ai.workbuddy.test.ping", {"x": 1}))
    assert isinstance(seq, int) and seq >= 1


def test_dedup_same_source_id_returns_none(fresh_store):
    s1 = fresh_store.append(
        new_event("/wb/test", "ai.workbuddy.test.ping", {"x": 1}, id="fixed-1")
    )
    s2 = fresh_store.append(
        new_event("/wb/test", "ai.workbuddy.test.ping", {"x": 2}, id="fixed-1")
    )
    assert s1 is not None
    assert s2 is None  # (source, id) is the inbox dedup claim


def test_read_since_ordered_tail(fresh_store):
    for i in range(3):
        fresh_store.append(new_event("/wb/test", "ai.workbuddy.test.ping", {"i": i}))
    out = fresh_store.read_since(0, limit=10)
    seqs = [s for s, _ in out]
    assert seqs == sorted(seqs)
    assert [e.data["i"] for _, e in out] == [0, 1, 2]
    # read_since is exclusive of last_seq
    assert [e.data["i"] for _, e in fresh_store.read_since(seqs[0])] == [1, 2]


def test_offsets_roundtrip(fresh_store):
    assert fresh_store.get_offset("c1") == 0  # default when absent
    fresh_store.commit_offset("c1", 5)
    assert fresh_store.get_offset("c1") == 5
    fresh_store.commit_offset("c1", 9)  # upsert
    assert fresh_store.get_offset("c1") == 9


def test_min_live_offset(fresh_store):
    assert fresh_store.min_live_offset() == sys.maxsize  # no consumers → pin nothing
    fresh_store.ensure_offset("c1", 0)
    assert fresh_store.min_live_offset() == 0  # registered but undelivered → pin all
    fresh_store.commit_offset("c1", 3)
    fresh_store.commit_offset("c2", 7)
    assert fresh_store.min_live_offset() == 3  # the slowest consumer


def test_event_roundtrips_full_envelope(fresh_store):
    e = new_event(
        "/wb/test",
        "ai.workbuddy.test.ping",
        {"a": "b"},
        subject="subj",
        modality="pull",
        traceparent="tp-1",
        idempotency_key="idem-1",
        ext={"custom": "x"},
    )
    fresh_store.append(e)
    [(seq, got)] = fresh_store.read_since(0)
    assert seq >= 1
    assert got.source == "/wb/test"
    assert got.type == "ai.workbuddy.test.ping"
    assert got.data == {"a": "b"}
    assert got.subject == "subj"
    assert got.modality == "pull"
    assert got.traceparent == "tp-1"
    assert got.idempotency_key == "idem-1"
    assert got.ext.get("custom") == "x"
    assert got.dedup_key == f"/wb/test::{e.id}"


def test_dead_letter(fresh_store):
    fresh_store.dead_letter(7, "c1", 3, "boom")
    assert fresh_store.dlq_seqs() == {7}
