"""Slice 5b: session_focus (working_on_now) CRUD + uniqueness + reverse lookup."""

from __future__ import annotations

import pytest

from work_buddy.obsidian.tasks import session_focus, store


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "tasks.sqlite3"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


def _seed(task_id: str = "t-foc-1"):
    store.create(task_id=task_id, state="focused")
    return task_id


def test_set_and_get_round_trip(fresh_db):
    _seed("t-a")
    out = session_focus.set_working_on_now("sess-1", "t-a")
    assert out["task_id"] == "t-a"
    assert out["session_id"] == "sess-1"
    fetched = session_focus.get_working_on_now("sess-1")
    assert fetched["task_id"] == "t-a"


def test_set_replaces_per_session(fresh_db):
    _seed("t-a")
    _seed("t-b")
    session_focus.set_working_on_now("sess-1", "t-a")
    session_focus.set_working_on_now("sess-1", "t-b")
    assert session_focus.get_working_on_now("sess-1")["task_id"] == "t-b"


def test_two_sessions_independent(fresh_db):
    _seed("t-a")
    _seed("t-b")
    session_focus.set_working_on_now("sess-1", "t-a")
    session_focus.set_working_on_now("sess-2", "t-b")
    assert session_focus.get_working_on_now("sess-1")["task_id"] == "t-a"
    assert session_focus.get_working_on_now("sess-2")["task_id"] == "t-b"


def test_clear_returns_true_then_false(fresh_db):
    _seed("t-a")
    session_focus.set_working_on_now("sess-1", "t-a")
    assert session_focus.clear_working_on_now("sess-1") is True
    assert session_focus.clear_working_on_now("sess-1") is False
    assert session_focus.get_working_on_now("sess-1") is None


def test_sessions_focused_on_reverse_lookup(fresh_db):
    _seed("t-a")
    _seed("t-b")
    session_focus.set_working_on_now("s1", "t-a")
    session_focus.set_working_on_now("s2", "t-a")
    session_focus.set_working_on_now("s3", "t-b")
    on_a = session_focus.sessions_focused_on("t-a")
    assert {r["session_id"] for r in on_a} == {"s1", "s2"}
    assert len(session_focus.sessions_focused_on("t-b")) == 1


def test_all_active_returns_every_focus_row(fresh_db):
    _seed("t-a")
    _seed("t-b")
    session_focus.set_working_on_now("s1", "t-a")
    session_focus.set_working_on_now("s2", "t-b")
    rows = session_focus.all_active()
    assert {r["session_id"] for r in rows} == {"s1", "s2"}


def test_set_validates_inputs(fresh_db):
    with pytest.raises(ValueError):
        session_focus.set_working_on_now("", "t-a")
    with pytest.raises(ValueError):
        session_focus.set_working_on_now("s1", "")


def test_clear_handles_missing_session(fresh_db):
    assert session_focus.clear_working_on_now("never") is False


def test_get_handles_missing_session(fresh_db):
    assert session_focus.get_working_on_now("nope") is None


def test_focus_row_persists_after_task_delete(fresh_db):
    """SQLite FKs aren't enforced without per-connection PRAGMA;
    explicit clear_working_on_now is the canonical cleanup path
    (per session_focus.py docstring).  This test pins that
    documented behavior so an accidental future change to enable
    PRAGMA foreign_keys=ON is caught."""
    _seed("t-a")
    session_focus.set_working_on_now("s1", "t-a")
    store.delete("t-a")
    # Row remains; explicit clear is what removes it.
    assert session_focus.get_working_on_now("s1") is not None
    assert session_focus.clear_working_on_now("s1") is True
