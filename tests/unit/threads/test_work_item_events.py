"""Phase-4 base event log: emit/list primitive + the additive wiring on
the task mutation hook.

The log is the WorkItem base's provenance record (separate store, no FK)
so any subtype can emit — today Task, whose rows live outside the threads
table. Emission is best-effort (never breaks a mutation) and additive
(markdown stays source of truth).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.threads import work_item_events as wie


@pytest.fixture
def fresh_events_db(tmp_path: Path, monkeypatch):
    db = tmp_path / "work_item_events.db"
    monkeypatch.setattr(wie, "_db_path", lambda: db)
    return db


def test_emit_and_list_round_trip(fresh_events_db):
    rid = wie.emit(
        "t-abc12345", "task.state_changed",
        subtype="task", actor="user", origin="task_mutation",
        data={"state": "done", "reason": "toggled"},
    )
    assert isinstance(rid, int)
    events = wie.list_events("t-abc12345")
    assert len(events) == 1
    e = events[0]
    assert e["work_item_id"] == "t-abc12345"
    assert e["kind"] == "task.state_changed"
    assert e["subtype"] == "task"
    assert e["actor"] == "user"
    assert e["origin"] == "task_mutation"
    assert e["data"] == {"state": "done", "reason": "toggled"}
    assert e["timestamp"]


def test_list_is_oldest_first(fresh_events_db):
    wie.emit("t-x", "task.created", subtype="task")
    wie.emit("t-x", "task.state_changed", subtype="task", data={"state": "focused"})
    wie.emit("t-x", "task.state_changed", subtype="task", data={"state": "done"})
    kinds = [e["kind"] for e in wie.list_events("t-x")]
    assert kinds == ["task.created", "task.state_changed", "task.state_changed"]


def test_events_are_scoped_per_work_item(fresh_events_db):
    wie.emit("t-a", "task.created", subtype="task")
    wie.emit("t-b", "task.created", subtype="task")
    assert len(wie.list_events("t-a")) == 1
    assert len(wie.list_events("t-b")) == 1
    assert wie.list_events("t-missing") == []


def test_emit_is_best_effort_never_raises(fresh_events_db, monkeypatch):
    # If the DB layer blows up, emit must swallow it and return None —
    # a missed audit event must never break the task mutation that fired it.
    def boom():
        raise RuntimeError("disk gone")

    monkeypatch.setattr(wie, "get_connection", boom)
    assert wie.emit("t-x", "task.created") is None  # no exception


def test_list_is_best_effort_never_raises(fresh_events_db, monkeypatch):
    monkeypatch.setattr(wie, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert wie.list_events("t-x") == []


def test_task_mutation_hook_emits_a_work_item_event(fresh_events_db, monkeypatch):
    # The additive wiring: _publish_task_event (fired by create/toggle/
    # update/description) also records a durable WorkItem event. The
    # dashboard publish + actor detection are best-effort, so this works
    # without a running dashboard.
    from work_buddy.obsidian.tasks import mutations

    mutations._publish_task_event(
        "task.state_changed", {"task_id": "t-hook01", "state": "done", "reason": "toggled"},
    )
    events = wie.list_events("t-hook01")
    assert len(events) == 1
    assert events[0]["kind"] == "task.state_changed"
    assert events[0]["subtype"] == "task"
    assert events[0]["origin"] == "task_mutation"
    assert events[0]["data"]["state"] == "done"


def test_hook_without_task_id_does_not_emit(fresh_events_db):
    from work_buddy.obsidian.tasks import mutations

    # A payload with no task_id (defensive) emits nothing rather than
    # recording a junk row.
    mutations._publish_task_event("task.something", {"no_id": True})
    assert wie.list_events("") == []
