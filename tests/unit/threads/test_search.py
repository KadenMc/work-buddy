"""v5 Stage 4.8 — search + filters."""

from __future__ import annotations

import pytest

from work_buddy.threads import search, store
from work_buddy.threads.events import (
    KIND_ACTION_INFERRED,
    KIND_INTENT_INFERRED,
    ThreadEvent,
)
from work_buddy.threads.models import ContextItem, Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


# ---------------------------------------------------------------------------
# build_search_blob
# ---------------------------------------------------------------------------


class TestBuildSearchBlob:
    def test_includes_inciting_description(self, fresh_db):
        t = Thread(inciting_event_summary={
            "description": "Sarah's birthday + gift",
        })
        store.insert_thread(t)
        blob = search.build_search_blob(t)
        assert "sarah" in blob
        assert "birthday" in blob

    def test_includes_context_item_labels(self, fresh_db):
        t = Thread(context_items=(
            ContextItem(id="ci1", source="x", type="y", label="ECG paper draft"),
            ContextItem(id="ci2", source="x", type="y", label="Slack thread"),
        ))
        store.insert_thread(t)
        blob = search.build_search_blob(t)
        assert "ecg paper draft" in blob
        assert "slack thread" in blob

    def test_includes_latest_intent(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_INTENT_INFERRED,
            actor="agent",
            data={"payload": {"intent": "schedule a meeting"}},
        ))
        blob = search.build_search_blob(t)
        assert "schedule a meeting" in blob

    def test_includes_action_name_and_summary(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={"payload": {
                "kind": "standard",
                "name": "send_email",
                "plan_summary": "Email Anna about the draft",
                "parameters": {"subject": "Re: ECG paper"},
            }},
        ))
        blob = search.build_search_blob(t)
        assert "send_email" in blob
        assert "email anna about the draft" in blob
        assert "re: ecg paper" in blob

    def test_lowercased(self, fresh_db):
        t = Thread(inciting_event_summary={"description": "UPPERCASE"})
        store.insert_thread(t)
        blob = search.build_search_blob(t)
        assert blob == blob.lower()


# ---------------------------------------------------------------------------
# update_search_blob persists
# ---------------------------------------------------------------------------


class TestUpdateSearchBlob:
    def test_updates_threads_row(self, fresh_db):
        t = Thread(inciting_event_summary={"description": "find me"})
        store.insert_thread(t)
        search.update_search_blob(t.thread_id)
        fetched = store.get_thread(t.thread_id)
        assert "find me" in fetched.search_blob

    def test_unknown_thread_returns_none(self, fresh_db):
        assert search.update_search_blob("th-missing") is None


# ---------------------------------------------------------------------------
# search_threads
# ---------------------------------------------------------------------------


class TestSearchThreads:
    def _make_thread(self, description, **kwargs):
        t = Thread(inciting_event_summary={"description": description}, **kwargs)
        store.insert_thread(t)
        return t

    def test_substring_match_top_level(self, fresh_db):
        a = self._make_thread("Sarah's birthday")
        b = self._make_thread("Anna's deadline")
        c = self._make_thread("Bob's housewarming")
        # store.insert_thread already populates search_blob
        out = search.search_threads("sarah")
        assert {t.thread_id for t in out} == {a.thread_id}

    def test_case_insensitive(self, fresh_db):
        self._make_thread("Sarah's birthday")
        out = search.search_threads("SARAH")
        assert len(out) == 1

    def test_empty_query_returns_all_top_level(self, fresh_db):
        a = self._make_thread("a")
        b = self._make_thread("b")
        out = search.search_threads("")
        assert {t.thread_id for t in out} == {a.thread_id, b.thread_id}

    def test_state_filter(self, fresh_db):
        a = self._make_thread("a")
        store.update_thread_state(a.thread_id, fsm_state="awaiting_review")
        b = self._make_thread("b")
        out = search.search_threads("", state="awaiting_review")
        assert {t.thread_id for t in out} == {a.thread_id}

    def test_subtype_filter(self, fresh_db):
        a = self._make_thread("a", subtype="task")
        b = self._make_thread("b")
        out = search.search_threads("", subtype="task")
        assert {t.thread_id for t in out} == {a.thread_id}

    def test_show_later_default_excludes_future(self, fresh_db):
        from datetime import datetime, timedelta, timezone
        a = self._make_thread("a")
        future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        store.update_thread_state(a.thread_id, resurface_at=future)
        out = search.search_threads("")
        assert all(t.thread_id != a.thread_id for t in out)
        # With show_later=True, included
        out2 = search.search_threads("", show_later=True)
        assert any(t.thread_id == a.thread_id for t in out2)

    def test_top_level_only_excludes_children(self, fresh_db):
        p = self._make_thread("parent")
        c = Thread(parent_id=p.thread_id, inciting_event_summary={"description": "child"})
        store.insert_thread(c)
        out = search.search_threads("")
        # Only parent, not child
        assert {t.thread_id for t in out} == {p.thread_id}

    def test_parent_id_lists_only_children(self, fresh_db):
        p = self._make_thread("parent")
        c1 = Thread(parent_id=p.thread_id, inciting_event_summary={"description": "child 1"})
        c2 = Thread(parent_id=p.thread_id, inciting_event_summary={"description": "child 2"})
        store.insert_thread(c1)
        store.insert_thread(c2)
        out = search.search_threads("", parent_id=p.thread_id)
        assert {t.thread_id for t in out} == {c1.thread_id, c2.thread_id}
