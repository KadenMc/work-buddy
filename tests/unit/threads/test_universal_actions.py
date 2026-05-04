"""Tests for ``work_buddy.threads.universal_actions`` — the dismiss /
defer / rename primitives backing the universal action library.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import models, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import KIND_THREAD_RENAMED
from work_buddy.threads.universal_actions import (
    UniversalActionError,
    thread_defer,
    thread_dismiss,
    thread_rename,
)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Per-test threads DB."""
    threads_db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: threads_db)
    yield


def _make_thread(state=FSMState.AWAITING_CONFIRMATION, title="Test thread"):
    t = models.Thread(
        fsm_state=state,
        inciting_event_summary={"title": title, "description": title},
    )
    store.insert_thread(t)
    return t


# ---------------------------------------------------------------------------
# thread_dismiss
# ---------------------------------------------------------------------------


class TestThreadDismiss:
    def test_dismiss_transitions_to_terminal(self, fresh_db):
        t = _make_thread()
        result = thread_dismiss(t.thread_id, reason="cluster_wrong")
        assert result["new_state"] == "dismissed"
        after = store.get_thread(t.thread_id)
        assert after.fsm_state == FSMState.DISMISSED

    def test_dismiss_records_reason_in_data(self, fresh_db):
        t = _make_thread()
        thread_dismiss(t.thread_id, reason="my_reason")
        events = store.list_events(t.thread_id)
        # The state_transition event recorded by engine.transition
        # carries the reason in its data.
        dismiss_events = [
            e for e in events
            if e.kind in ("state_transition", "thread_dismissed")
        ]
        assert any(
            e.data.get("reason") == "my_reason"
            or e.data.get("trigger_data", {}).get("reason") == "my_reason"
            for e in dismiss_events
        )

    def test_dismiss_missing_thread_raises(self, fresh_db):
        with pytest.raises(UniversalActionError, match="not found"):
            thread_dismiss("th-doesnotexist")

    def test_dismiss_already_terminal_raises(self, fresh_db):
        t = _make_thread(state=FSMState.DONE)
        with pytest.raises(UniversalActionError, match="already terminal"):
            thread_dismiss(t.thread_id)


# ---------------------------------------------------------------------------
# thread_defer
# ---------------------------------------------------------------------------


class TestThreadDefer:
    def test_defer_default_24h(self, fresh_db):
        t = _make_thread()
        result = thread_defer(t.thread_id)
        assert result["resurface_at"]
        after = store.get_thread(t.thread_id)
        assert after.resurface_at == result["resurface_at"]

    def test_defer_custom_duration(self, fresh_db):
        t = _make_thread()
        result = thread_defer(t.thread_id, duration_hours=2.0)
        assert result["resurface_at"]
        # Audit event recorded as 'later'
        events = store.list_events(t.thread_id)
        later = [e for e in events if e.kind == "later"]
        assert len(later) == 1
        assert later[0].data["resurface_at"] == result["resurface_at"]

    def test_defer_explicit_iso(self, fresh_db):
        t = _make_thread()
        explicit = "2026-12-31T00:00:00+00:00"
        result = thread_defer(t.thread_id, resurface_at=explicit)
        assert result["resurface_at"] == explicit

    def test_defer_missing_thread_raises(self, fresh_db):
        with pytest.raises(UniversalActionError, match="not found"):
            thread_defer("th-doesnotexist")


# ---------------------------------------------------------------------------
# thread_rename
# ---------------------------------------------------------------------------


class TestThreadRename:
    def test_rename_updates_title_and_description(self, fresh_db):
        t = _make_thread(title="Old title")
        result = thread_rename(t.thread_id, new_title="New title")
        assert result["previous_title"] == "Old title"
        assert result["new_title"] == "New title"
        after = store.get_thread(t.thread_id)
        assert after.inciting_event_summary["title"] == "New title"
        assert after.inciting_event_summary["description"] == "New title"

    def test_rename_records_audit_event(self, fresh_db):
        t = _make_thread(title="Before")
        thread_rename(t.thread_id, new_title="After")
        events = store.list_events(t.thread_id)
        rename = [e for e in events if e.kind == KIND_THREAD_RENAMED]
        assert len(rename) == 1
        assert rename[0].data["previous_title"] == "Before"
        assert rename[0].data["new_title"] == "After"

    def test_rename_strips_whitespace(self, fresh_db):
        t = _make_thread()
        thread_rename(t.thread_id, new_title="   Spaced   ")
        after = store.get_thread(t.thread_id)
        assert after.inciting_event_summary["title"] == "Spaced"

    def test_rename_empty_title_raises(self, fresh_db):
        t = _make_thread()
        with pytest.raises(UniversalActionError, match="non-empty"):
            thread_rename(t.thread_id, new_title="   ")

    def test_rename_missing_thread_raises(self, fresh_db):
        with pytest.raises(UniversalActionError, match="not found"):
            thread_rename("th-doesnotexist", new_title="x")
