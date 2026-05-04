"""Unit tests for the v5 Threads tab dashboard endpoints.

Covers the routes added during the overnight UX overhaul (Wave A
through Wave G, 2026-05-03):

- ``GET /api/threads`` with the new filters: ``urgency``,
  ``has_cleanup``, ``show_all``, ``include_mid_process``.
- ``GET /api/threads/<id>/events`` event-log inspector backing.
- ``POST /api/run/<capability>`` allowlist gateway shim.

These tests exercise the route handlers directly via the Flask
test client + a tmp-path-isolated threads DB. They don't go
through the MCP gateway or the sidecar; the route handlers are
tested in isolation.
"""

from __future__ import annotations

import json

import pytest

from work_buddy.threads import autonomy, store
from work_buddy.threads.events import (
    KIND_ACTION_INFERRED,
    KIND_INTENT_INFERRED,
    ThreadEvent,
)
from work_buddy.threads.models import Thread


@pytest.fixture
def fresh_threads_db(tmp_path, monkeypatch):
    """Isolate the threads DB to a tmp path so tests don't see
    live data."""
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    yield db


@pytest.fixture
def client(fresh_threads_db):
    from work_buddy.dashboard.service import app
    yield app.test_client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_thread(
    *, urgency: str = "defer", source: str = "journal_note",
    fsm_state="awaiting_intent_confirmation",
    description: str = "test thread",
    autonomy_policy=None,
    cleanup_ready: bool = True,
):
    """Insert a thread with optional inciting urgency + source.

    ``cleanup_ready=True`` (default): journal_note threads include
    the note_path + line_text fields the cleanup adapter requires.
    Set False to test the "registered but not applicable" case.
    """
    from work_buddy.threads.enums import FSMState
    state = (
        fsm_state
        if isinstance(fsm_state, FSMState)
        else FSMState(fsm_state)
    )
    inciting = {
        "source": source,
        "description": description,
        "urgency": urgency,
    }
    if source == "journal_note" and cleanup_ready:
        inciting["note_path"] = "journal/2026-05-01.md"
        inciting["line_text"] = description
    t = Thread(
        fsm_state=state,
        inciting_event_summary=inciting,
        autonomy_policy=autonomy_policy or autonomy.PLAN_THEN_REVIEW,
    )
    store.insert_thread(t)
    return t


# ---------------------------------------------------------------------------
# /api/threads filter chips
# ---------------------------------------------------------------------------


class TestThreadsListFilters:
    def test_urgency_filter_surface_now(self, client):
        a = _make_thread(urgency="surface_now", description="urgent thing")
        b = _make_thread(urgency="defer", description="not urgent")
        resp = client.get("/api/threads?urgency=surface_now")
        assert resp.status_code == 200
        ids = [t["thread_id"] for t in resp.get_json()["threads"]]
        assert a.thread_id in ids
        assert b.thread_id not in ids

    def test_urgency_filter_defer(self, client):
        a = _make_thread(urgency="surface_now")
        b = _make_thread(urgency="defer")
        resp = client.get("/api/threads?urgency=defer")
        ids = [t["thread_id"] for t in resp.get_json()["threads"]]
        assert b.thread_id in ids
        assert a.thread_id not in ids

    def test_has_cleanup_filter(self, client, monkeypatch):
        """Hide threads where the cleanup adapter doesn't apply.
        Source 'journal_note' has a registered adapter; 'unknown'
        doesn't."""
        # Bootstrap registers the journal cleanup adapter
        from work_buddy.threads import cleanup_adapters
        cleanup_adapters.register_default_adapters()
        a = _make_thread(source="journal_note", description="has cleanup")
        b = _make_thread(source="unknown_source",
                         description="no cleanup")
        resp = client.get("/api/threads?has_cleanup=1")
        ids = [t["thread_id"] for t in resp.get_json()["threads"]]
        assert a.thread_id in ids
        assert b.thread_id not in ids
        # Without the filter, both surface (assuming the FSM state
        # is in the actionable set).
        resp_all = client.get("/api/threads")
        ids_all = [t["thread_id"] for t in resp_all.get_json()["threads"]]
        assert a.thread_id in ids_all
        assert b.thread_id in ids_all

    def test_show_all_disables_actionable_filter(self, client):
        """show_all=1 surfaces non-actionable states (e.g.
        terminal). Default behavior hides them."""
        from work_buddy.threads.enums import FSMState
        a = _make_thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION,
                         description="actionable")
        b = _make_thread(fsm_state=FSMState.DONE,
                         description="terminal")
        # Default: only the actionable thread.
        resp = client.get("/api/threads")
        ids = [t["thread_id"] for t in resp.get_json()["threads"]]
        assert a.thread_id in ids
        assert b.thread_id not in ids
        # With show_all: both.
        resp_all = client.get("/api/threads?show_all=1")
        ids_all = [t["thread_id"] for t in resp_all.get_json()["threads"]]
        assert a.thread_id in ids_all
        assert b.thread_id in ids_all

    def test_include_mid_process(self, client):
        """include_mid_process=1 surfaces inferring/executing/etc
        states without dropping the actionable_only filter."""
        from work_buddy.threads.enums import FSMState
        a = _make_thread(fsm_state=FSMState.INFERRING_INTENT,
                         description="in flight")
        # Default: hidden.
        resp = client.get("/api/threads")
        ids = [t["thread_id"] for t in resp.get_json()["threads"]]
        assert a.thread_id not in ids
        # Toggled on: visible.
        resp_mp = client.get("/api/threads?include_mid_process=1")
        ids_mp = [t["thread_id"] for t in resp_mp.get_json()["threads"]]
        assert a.thread_id in ids_mp


# ---------------------------------------------------------------------------
# /api/threads/<id>/events
# ---------------------------------------------------------------------------


class TestThreadEventsEndpoint:
    def test_returns_event_log(self, client):
        t = _make_thread()
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_INTENT_INFERRED,
            actor="agent",
            data={"payload": {"intent": "test"}, "confidence": 0.9},
        ))
        resp = client.get(f"/api/threads/{t.thread_id}/events")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["thread_id"] == t.thread_id
        # Some events including the intent_inferred we just wrote
        kinds = [e["kind"] for e in body["events"]]
        assert KIND_INTENT_INFERRED in kinds

    def test_unknown_thread_returns_empty_list(self, client):
        """Reading events for a thread that doesn't exist returns
        an empty list, not a 404 — store.list_events is permissive."""
        resp = client.get("/api/threads/th-does-not-exist/events")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["events"] == []

    def test_serialization_includes_inference_tier(self, client):
        t = _make_thread()
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            inference_tier="frontier_balanced",
            data={"payload": {"kind": "improvised", "name": "x"}},
        ))
        resp = client.get(f"/api/threads/{t.thread_id}/events")
        body = resp.get_json()
        action_event = next(e for e in body["events"]
                            if e["kind"] == KIND_ACTION_INFERRED)
        assert action_event["inference_tier"] == "frontier_balanced"


# ---------------------------------------------------------------------------
# /api/run/<capability> allowlist
# ---------------------------------------------------------------------------


class TestRunCapabilityEndpoint:
    def test_unknown_capability_rejected_403(self, client):
        resp = client.post("/api/run/some_random_capability",
                           json={})
        assert resp.status_code == 403
        body = resp.get_json()
        assert "not exposed to the dashboard" in body["error"].lower() \
            or "not exposed" in body["error"].lower()

    def test_journal_v5_scan_is_in_allowlist(self):
        """Whitelist regression: ``journal_v5_scan`` must remain
        in the allowlist; the empty-state CTA depends on it."""
        from work_buddy.dashboard.service import (
            _DASHBOARD_RUNNABLE_CAPABILITIES,
        )
        assert "journal_v5_scan" in _DASHBOARD_RUNNABLE_CAPABILITIES


# ---------------------------------------------------------------------------
# /api/threads risk_highlight passthrough
# ---------------------------------------------------------------------------


class TestRiskHighlightInList:
    def test_high_risk_action_surfaces_pill(self, client):
        """Render data should carry the risk_highlight at the
        top level so the list view can show a colored dot."""
        t = _make_thread()
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={"payload": {
                "kind": "improvised",
                "name": "delete drafts",
                "irreversibility": "high",
                "regret_potential": "high",
                "risk_amplifier": True,
            }},
        ))
        resp = client.get("/api/threads")
        threads = resp.get_json()["threads"]
        match = [x for x in threads if x["thread_id"] == t.thread_id]
        assert match
        assert match[0]["risk_highlight"] == "high"


# ---------------------------------------------------------------------------
# Stage 5 grouping endpoints
# ---------------------------------------------------------------------------


def _make_group_parent(scope: str = "scrape-A"):
    from work_buddy.threads.enums import FSMState
    t = Thread(
        fsm_state=FSMState.MONITORING,
        parent_relationship="group",
        originating_scrape_id=scope,
    )
    store.insert_thread(t)
    return t


def _make_child_under(parent, fsm_state="awaiting_confirmation"):
    from work_buddy.threads.enums import FSMState
    state = fsm_state if isinstance(fsm_state, FSMState) else FSMState(fsm_state)
    c = Thread(parent_id=parent.thread_id, fsm_state=state)
    store.insert_thread(c)
    return c


class TestMoveParentEndpoint:
    def test_happy_path_returns_200(self, client):
        g1 = _make_group_parent("scrape-A")
        g2 = _make_group_parent("scrape-A")
        c = _make_child_under(g1)
        # Keep g1 alive after the move so it doesn't auto-DISMISS.
        _make_child_under(g1)
        resp = client.post(
            f"/api/threads/{c.thread_id}/move_parent",
            data=json.dumps({"new_parent_id": g2.thread_id}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["from_parent"] == g1.thread_id
        assert body["to_parent"] == g2.thread_id
        assert body["migration_id"]

    def test_missing_body_returns_400(self, client):
        # Can't move without a destination parent.
        g = _make_group_parent("scrape-A")
        c = _make_child_under(g)
        resp = client.post(
            f"/api/threads/{c.thread_id}/move_parent",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "new_parent_id required" in resp.get_json()["error"]

    def test_scope_mismatch_returns_422(self, client):
        g1 = _make_group_parent("scrape-A")
        g2 = _make_group_parent("scrape-B")
        c = _make_child_under(g1)
        resp = client.post(
            f"/api/threads/{c.thread_id}/move_parent",
            data=json.dumps({"new_parent_id": g2.thread_id}),
            content_type="application/json",
        )
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["reason"] == "scope_mismatch"


class TestGroupSiblingsEndpoint:
    def test_returns_self_plus_siblings_with_children(self, client):
        g1 = _make_group_parent("scrape-A")
        g2 = _make_group_parent("scrape-A")
        c1 = _make_child_under(g1)
        c2 = _make_child_under(g2)
        resp = client.get(f"/api/threads/{g1.thread_id}/group_siblings")
        assert resp.status_code == 200
        body = resp.get_json()
        sibs = {s["thread_id"]: s for s in body["siblings"]}
        assert g1.thread_id in sibs
        assert g2.thread_id in sibs
        # Each sibling carries its children inline.
        sib_g1 = sibs[g1.thread_id]
        sib_g2 = sibs[g2.thread_id]
        assert "children_render" in sib_g1
        assert "children_render" in sib_g2
        assert {c["thread_id"] for c in sib_g1["children_render"]} == {c1.thread_id}
        assert {c["thread_id"] for c in sib_g2["children_render"]} == {c2.thread_id}

    def test_unknown_parent_returns_empty_list(self, client):
        resp = client.get("/api/threads/nonexistent/group_siblings")
        assert resp.status_code == 200
        assert resp.get_json()["siblings"] == []


class TestGroupSubmitEndpoint:
    def test_skip_count_for_non_awaiting(self, client, monkeypatch):
        from work_buddy.threads.enums import FSMState
        # Patch engine.transition so it doesn't try to fire side effects
        # against a non-bootstrapped engine.
        from unittest.mock import MagicMock
        from work_buddy.threads import engine
        fake = MagicMock()
        fake.next_state = FSMState.EXECUTING
        monkeypatch.setattr(engine, "transition", lambda *a, **k: fake)
        g = _make_group_parent("scrape-A")
        _make_child_under(g, FSMState.AWAITING_CONFIRMATION)
        _make_child_under(g, FSMState.PROPOSED)
        resp = client.post(
            f"/api/threads/{g.thread_id}/group_submit",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["submitted"] == 1
        assert body["skipped"] == 1
        assert body["failed"] == 0

    def test_decompose_parent_rejected_with_422(self, client):
        from work_buddy.threads.enums import FSMState
        d = Thread(fsm_state=FSMState.MONITORING)
        store.insert_thread(d)
        resp = client.post(
            f"/api/threads/{d.thread_id}/group_submit",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 422
        assert resp.get_json()["reason"] == "parent_not_group"
