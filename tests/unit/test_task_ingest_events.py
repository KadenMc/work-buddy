"""External-markdown-edit ingest: the task reconciler records hand-edits
(made directly in Obsidian) into the WorkItem audit log.

Agent/code mutations go through mutations.py (which emits its own
origin='task_mutation' events and leaves no drift). A *drift* the
reconciler resolves in markdown's favour is therefore an external edit —
it must surface as a WorkItem event with actor='user',
origin='external_markdown'. Store-wins drift is NOT an external edit and
must emit nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from work_buddy.markdown_db.types import ReconcileReport
from work_buddy.obsidian.tasks import store as task_store
from work_buddy.obsidian.tasks.markdown_db import TaskMarkdownDB
from work_buddy.threads import work_item_events as wie


@pytest.fixture
def fresh_events_db(tmp_path: Path, monkeypatch):
    # Explicit isolation (belt-and-suspenders over the autouse conftest one).
    monkeypatch.setattr(wie, "_db_path", lambda: tmp_path / "wie.db")
    return tmp_path


def _report(*, created=(), deleted=(), drift=None) -> ReconcileReport:
    r = ReconcileReport()
    r.created = list(created)
    r.deleted = list(deleted)
    r.drift = drift or {}
    return r


def test_markdown_wins_drift_emits_user_ingest_event(fresh_events_db):
    report = _report(drift={
        "checkbox": [{"pk": "t-chg01", "old": "inbox", "new": "done", "winner": "markdown"}],
    })
    TaskMarkdownDB(task_store)._emit_ingest_events(report)

    events = wie.list_events("t-chg01")
    assert len(events) == 1
    e = events[0]
    assert e["kind"] == "task.ingested_changed"
    assert e["actor"] == "user"
    assert e["origin"] == "external_markdown"
    assert e["subtype"] == "task"
    assert e["data"]["field"] == "checkbox"
    assert e["data"]["old"] == "inbox"
    assert e["data"]["new"] == "done"


def test_store_wins_drift_emits_nothing(fresh_events_db):
    # The store won the conflict — that's not an external markdown edit, so
    # no ingest event (the agent-side change already logged its own event).
    report = _report(drift={
        "urgency": [{"pk": "t-chg02", "old": "high", "new": "low", "winner": "store"}],
    })
    TaskMarkdownDB(task_store)._emit_ingest_events(report)
    assert wie.list_events("t-chg02") == []


def test_created_and_deleted_orphans_emit_ingest_events(fresh_events_db):
    report = _report(created=["t-new01"], deleted=["t-del01"])
    TaskMarkdownDB(task_store)._emit_ingest_events(report)

    created = wie.list_events("t-new01")
    deleted = wie.list_events("t-del01")
    assert [e["kind"] for e in created] == ["task.ingested_created"]
    assert [e["kind"] for e in deleted] == ["task.ingested_deleted"]
    assert created[0]["origin"] == "external_markdown"
    assert deleted[0]["actor"] == "user"


def test_emission_is_best_effort(fresh_events_db, monkeypatch):
    # If the event layer raises, the reconcile post-pass must not blow up.
    monkeypatch.setattr(
        wie, "emit",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    # Should not raise.
    TaskMarkdownDB(task_store)._emit_ingest_events(
        _report(created=["t-x"]),
    )
