"""Unit tests for the events dispatcher — publish + drain."""

from __future__ import annotations

import pytest

import work_buddy.dashboard.events as dash_events
from work_buddy.events import dispatcher
from work_buddy.events import store as evstore
from work_buddy.events.dispatcher import DurableConsumer
from work_buddy.events.envelope import new_event
from work_buddy.events.protocol import ProcessorManifest, ProcessorResult


class _Recorder:
    def __init__(self):
        self.manifest = ProcessorManifest(name="rec")
        self.seen: list[str] = []

    def run(self, event, ctx):
        self.seen.append(event.id)
        return ProcessorResult(text="ok")


class _Exploder:
    def __init__(self):
        self.manifest = ProcessorManifest(name="boom")
        self.calls = 0

    def run(self, event, ctx):
        self.calls += 1
        raise RuntimeError("nope")


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(evstore, "_db_path", lambda: tmp_path / "events.db")
    store = evstore.EventStore()
    dispatcher.set_store(store)
    dispatcher.clear_consumers()
    fanout: list[tuple] = []
    monkeypatch.setattr(
        dash_events, "publish_auto", lambda t, p=None: fanout.append((t, p))
    )
    yield store, fanout
    dispatcher.clear_consumers()
    dispatcher.set_store(None)


def _ping(id_: str | None = None):
    return new_event("/wb/test", "ai.workbuddy.demo.ping", {"k": "v"}, id=id_)


def test_publish_appends_and_fans_out(wired):
    store, fanout = wired
    dispatcher.publish(_ping("p1"))
    assert store.count() == 1
    assert fanout and fanout[0][0] == "ai.workbuddy.demo.ping"


def test_publish_dedup_no_double_append(wired):
    store, _ = wired
    dispatcher.publish(_ping("dup"))
    dispatcher.publish(_ping("dup"))
    assert store.count() == 1


def test_durable_false_skips_log_but_fans_out(wired):
    store, fanout = wired
    dispatcher.publish(
        new_event("/wb/test", "ai.workbuddy.ui.refresh", {}, durable=False)
    )
    assert store.count() == 0
    assert len(fanout) == 1


def test_drain_delivers_exactly_once(wired):
    store, _ = wired
    proc = _Recorder()
    dispatcher.register_consumer(
        DurableConsumer(id="c1", processor=proc, types=frozenset({"ai.workbuddy.demo.ping"}))
    )
    e = _ping("p1")
    dispatcher.publish(e)
    assert dispatcher.drain()["delivered"] == 1
    assert proc.seen == [e.id]
    dispatcher.drain()  # re-drain must not re-deliver
    assert proc.seen == [e.id]


def test_drain_type_filter_advances_offset(wired):
    store, _ = wired
    proc = _Recorder()
    dispatcher.register_consumer(
        DurableConsumer(id="c1", processor=proc, types=frozenset({"ai.workbuddy.demo.ping"}))
    )
    dispatcher.publish(new_event("/wb/test", "ai.workbuddy.schedule.tick", {}))
    dispatcher.publish(_ping("p1"))
    dispatcher.drain()
    assert len(proc.seen) == 1  # only the ping ran
    assert store.get_offset("c1") == store.max_seq()  # but offset advanced past both


def test_drain_poison_event_goes_to_dlq(wired, monkeypatch):
    store, _ = wired
    monkeypatch.setattr(dispatcher, "MAX_ATTEMPTS", 3)
    proc = _Exploder()
    dispatcher.register_consumer(
        DurableConsumer(id="c1", processor=proc, types=frozenset({"ai.workbuddy.demo.ping"}))
    )
    seq = store.append(_ping("boom"))
    for _ in range(3):  # attempt 1, 2, then 3 → DLQ
        dispatcher.drain()
    assert seq in store.dlq_seqs()
    assert store.get_offset("c1") == seq  # advanced after dead-lettering
    calls_before = proc.calls
    dispatcher.drain()  # no further runs
    assert proc.calls == calls_before


def test_offset_survives_restart(wired):
    store, _ = wired
    proc = _Recorder()
    dispatcher.register_consumer(DurableConsumer(id="c1", processor=proc, types=None))
    dispatcher.publish(_ping("p1"))
    dispatcher.drain()
    assert len(proc.seen) == 1
    # Simulate a restart: drop in-memory registry, re-register a fresh
    # processor against the SAME store. Offsets live in the DB → no replay.
    dispatcher.clear_consumers()
    proc2 = _Recorder()
    dispatcher.register_consumer(DurableConsumer(id="c1", processor=proc2, types=None))
    dispatcher.drain()
    assert proc2.seen == []
