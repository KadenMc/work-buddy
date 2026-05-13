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
        out = search.search_threads("sarah", actionable_only=False)
        assert {t.thread_id for t in out} == {a.thread_id}

    def test_case_insensitive(self, fresh_db):
        self._make_thread("Sarah's birthday")
        out = search.search_threads("SARAH", actionable_only=False)
        assert len(out) == 1

    def test_empty_query_returns_all_top_level(self, fresh_db):
        a = self._make_thread("a")
        b = self._make_thread("b")
        out = search.search_threads("", actionable_only=False)
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
        out = search.search_threads("", subtype="task", actionable_only=False)
        assert {t.thread_id for t in out} == {a.thread_id}

    def test_show_later_default_excludes_future(self, fresh_db):
        from datetime import datetime, timedelta, timezone
        a = self._make_thread("a")
        future = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        store.update_thread_state(a.thread_id, resurface_at=future)
        out = search.search_threads("", actionable_only=False)
        assert all(t.thread_id != a.thread_id for t in out)
        # With show_later=True, included
        out2 = search.search_threads("", show_later=True, actionable_only=False)
        assert any(t.thread_id == a.thread_id for t in out2)

    def test_top_level_only_excludes_children(self, fresh_db):
        p = self._make_thread("parent")
        c = Thread(parent_id=p.thread_id, inciting_event_summary={"description": "child"})
        store.insert_thread(c)
        out = search.search_threads("", actionable_only=False)
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

    def test_actionable_only_excludes_proposed_by_default(self, fresh_db):
        """Default actionable_only=True hides PROPOSED, INFERRING_*,
        EXECUTING, MONITORING, and terminal states."""
        proposed = self._make_thread("a")  # default PROPOSED
        wait = self._make_thread("b")
        store.update_thread_state(
            wait.thread_id, fsm_state="awaiting_intent_confirmation",
        )
        out = search.search_threads("")
        assert {t.thread_id for t in out} == {wait.thread_id}

    def test_explicit_state_filter_overrides_actionable_only(self, fresh_db):
        """Setting state='proposed' returns PROPOSED threads even though
        actionable_only is the default. Filter chips imply user opt-in."""
        proposed = self._make_thread("a")
        out = search.search_threads("", state="proposed")
        assert {t.thread_id for t in out} == {proposed.thread_id}

    def test_include_mid_process_adds_inferring_states(self, fresh_db):
        """Phase 4: include_mid_process=True surfaces threads in
        mid-flight states (AWAITING_INFERENCE, INFERRING_*,
        EXECUTING, MONITORING, CLEANING_UP) on top of the default
        actionable list."""
        actionable = self._make_thread("a")
        store.update_thread_state(
            actionable.thread_id, fsm_state="awaiting_intent_confirmation",
        )
        in_flight = self._make_thread("b")
        store.update_thread_state(
            in_flight.thread_id, fsm_state="inferring_intent",
        )

        # Default: actionable only — in-flight thread is hidden.
        out_default = search.search_threads("")
        assert {t.thread_id for t in out_default} == {actionable.thread_id}

        # With the toggle: both surface.
        out_toggled = search.search_threads("", include_mid_process=True)
        assert {t.thread_id for t in out_toggled} == {
            actionable.thread_id, in_flight.thread_id,
        }

    def test_include_mid_process_no_effect_when_actionable_only_false(self, fresh_db):
        """If the user already disabled actionable_only (full list),
        include_mid_process is a no-op — the broader filter wins."""
        proposed = self._make_thread("a")
        out = search.search_threads(
            "", actionable_only=False, include_mid_process=True,
        )
        assert {t.thread_id for t in out} == {proposed.thread_id}


# ---------------------------------------------------------------------------
# Pagination (limit + offset) and count_threads
# ---------------------------------------------------------------------------


class TestPagination:
    def _make_thread(self, description, **kwargs):
        t = Thread(inciting_event_summary={"description": description}, **kwargs)
        store.insert_thread(t)
        return t

    def test_offset_skips_matching_rows(self, fresh_db):
        """``offset=N`` returns the next page; combining with ``limit``
        produces non-overlapping page windows over the same result set."""
        for i in range(5):
            self._make_thread(f"t-{i}")
        page1 = search.search_threads(
            "", actionable_only=False, limit=2, offset=0,
        )
        page2 = search.search_threads(
            "", actionable_only=False, limit=2, offset=2,
        )
        page3 = search.search_threads(
            "", actionable_only=False, limit=2, offset=4,
        )
        ids1 = [t.thread_id for t in page1]
        ids2 = [t.thread_id for t in page2]
        ids3 = [t.thread_id for t in page3]
        assert len(ids1) == 2
        assert len(ids2) == 2
        assert len(ids3) == 1
        # No overlap across pages
        assert not (set(ids1) & set(ids2))
        assert not (set(ids2) & set(ids3))
        # Union of all pages equals the full result set
        assert set(ids1) | set(ids2) | set(ids3) == {
            t.thread_id for t in search.search_threads(
                "", actionable_only=False, limit=100,
            )
        }

    def test_offset_beyond_total_returns_empty(self, fresh_db):
        for i in range(3):
            self._make_thread(f"t-{i}")
        out = search.search_threads(
            "", actionable_only=False, limit=10, offset=100,
        )
        assert out == []


class TestCountThreads:
    def _make_thread(self, description, **kwargs):
        t = Thread(inciting_event_summary={"description": description}, **kwargs)
        store.insert_thread(t)
        return t

    def test_count_matches_search_total(self, fresh_db):
        """The count must equal the list length when no limit is hit —
        otherwise the pager would report a wrong total."""
        for i in range(7):
            self._make_thread(f"t-{i}")
        listed = search.search_threads(
            "", actionable_only=False, limit=100,
        )
        counted = search.count_threads("", actionable_only=False)
        assert counted == len(listed) == 7

    def test_count_respects_filters(self, fresh_db):
        a = self._make_thread("alpha")
        b = self._make_thread("beta")
        store.update_thread_state(b.thread_id, fsm_state="awaiting_review")
        # State filter narrows to one
        assert search.count_threads("", state="awaiting_review") == 1
        # Actionable-only with no wait-state threads → zero
        # (a is still PROPOSED, b is in awaiting_review which IS a wait
        # state, so the actionable-only count includes b.)
        assert search.count_threads("") == 1
        # Full count (no actionable filter) → both
        assert search.count_threads("", actionable_only=False) == 2

    def test_count_query_match(self, fresh_db):
        self._make_thread("Sarah's birthday")
        self._make_thread("Anna's deadline")
        self._make_thread("Sarah's anniversary")
        # Two threads contain "sarah"
        assert search.count_threads(
            "sarah", actionable_only=False,
        ) == 2
