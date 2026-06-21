"""End-to-end (in-process) test of the assembled events spine.

Wires the REAL EventStore + dispatcher + notify-demo consumer against temp
paths, with only the two external side effects stubbed (the dashboard fan-out
and the notification write are recorded, not performed). Covers acceptance-gate
items (a) log+dedup, (b) delivered exactly once, and the type-filter behavior.
"""

from __future__ import annotations

import pytest

import work_buddy.dashboard.events as dash_events
import work_buddy.notifications.dispatcher as ndisp
import work_buddy.notifications.store as nstore
from work_buddy.events import dispatcher
from work_buddy.events import store as evstore
from work_buddy.events.consumers.notify_demo import (
    CONSUMER_ID,
    PING_TYPE,
    register_notify_demo,
)
from work_buddy.events.envelope import new_event


@pytest.fixture
def spine(tmp_path, monkeypatch):
    monkeypatch.setattr(evstore, "_db_path", lambda: tmp_path / "events.db")
    dispatcher.set_store(evstore.EventStore())
    dispatcher.clear_consumers()
    fan: list[str] = []
    notes: list = []
    monkeypatch.setattr(dash_events, "publish_auto", lambda t, p=None: fan.append(t))
    monkeypatch.setattr(nstore, "create_notification", lambda n: notes.append(n) or n)

    # Keep the test hermetic: stub surface delivery so the consumer never makes
    # a real network call to Telegram/dashboard.
    class _NoDeliver:
        def deliver(self, notif, mark_delivered_fn=None):
            return {}

    monkeypatch.setattr(
        ndisp.SurfaceDispatcher, "from_config", classmethod(lambda cls: _NoDeliver())
    )
    register_notify_demo()
    yield dispatcher._store(), fan, notes
    dispatcher.clear_consumers()
    dispatcher.set_store(None)


@pytest.mark.component
def test_demo_ping_end_to_end(spine):
    store, fan, notes = spine

    dispatcher.publish(new_event("/wb/agent", PING_TYPE, {"message": "hi"}, id="e1"))
    assert store.count() == 1            # (a) landed in db/events
    assert fan == [PING_TYPE]            # fanned out to the projection

    dispatcher.drain()
    assert len(notes) == 1               # (b) delivered → notification written
    assert "hi" in notes[0].body

    dispatcher.drain()                   # re-drain must not re-deliver
    assert len(notes) == 1               # (b) exactly once

    dispatcher.publish(new_event("/wb/agent", PING_TYPE, {}, id="e1"))
    assert store.count() == 1            # (a) dedup on (source, id)


@pytest.mark.component
def test_schedule_tick_advances_offset_without_notifying(spine):
    store, _fan, notes = spine

    dispatcher.publish(new_event("/wb/scheduler", "ai.workbuddy.schedule.tick", {}))
    dispatcher.drain()

    assert notes == []                                       # type-filtered out
    assert store.get_offset(CONSUMER_ID) == store.max_seq()  # but offset advanced


def _source_fm(payload):
    return {
        "kind": "event_source",
        "source": {"type": "fake", "url": "x", "interval": "5m"},
        "extract": {"mode": "json_path", "path": "$.ceo"},
        "condition": "event.data != prev.data",
        "action": {"name": "notify"},
        "allowed_actions": ["notify"],
        "enabled": True,
        "fake_payload": payload,
    }


@pytest.mark.component
def test_source_change_reacts_end_to_end(tmp_path, monkeypatch):
    """A real poll → publish source.changed → drain → source-action consumer
    evaluates the condition and notifies — the full source reaction path."""
    import work_buddy.events.sources.loader as loader_mod
    from work_buddy.events.consumers.source_action import register_source_action
    from work_buddy.events.sources import poller as P
    from work_buddy.events.sources.definition import from_frontmatter

    monkeypatch.setattr(evstore, "_db_path", lambda: tmp_path / "events.db")
    dispatcher.set_store(evstore.EventStore())
    dispatcher.clear_consumers()
    notes: list = []
    monkeypatch.setattr(dash_events, "publish_auto", lambda t, p=None: None)
    monkeypatch.setattr(nstore, "create_notification", lambda n: notes.append(n) or n)

    class _NoDeliver:
        def deliver(self, notif, mark_delivered_fn=None):
            return {}

    monkeypatch.setattr(
        ndisp.SurfaceDispatcher, "from_config", classmethod(lambda cls: _NoDeliver())
    )

    src_a = from_frontmatter("nvda", _source_fm({"ceo": "A"}))
    src_b = from_frontmatter("nvda", _source_fm({"ceo": "B"}))
    # The consumer resolves the def by name; condition/action are identical, so
    # returning the baseline def is fine (only fetch uses fake_payload).
    monkeypatch.setattr(loader_mod, "load_event_sources", lambda directory=None: ([src_a], []))
    register_source_action()

    state_dir = tmp_path / "state"
    P.poll_source(src_a, state_directory=state_dir)   # baseline: silent, no emit
    dispatcher.drain()
    assert dispatcher._store().count() == 0
    assert notes == []

    P.poll_source(src_b, state_directory=state_dir)   # change: publishes source.changed
    assert dispatcher._store().count() == 1
    dispatcher.drain()                                # consumer reacts → notify
    assert len(notes) == 1
    assert "nvda" in notes[0].title

    dispatcher.drain()                                # exactly once
    assert len(notes) == 1

    dispatcher.clear_consumers()
    dispatcher.set_store(None)
