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

    def test_pagination_envelope_shape(self, client):
        """The list endpoint returns ``{threads, total, offset, limit}``
        — the dashboard pager depends on the envelope to size pages."""
        from work_buddy.threads.enums import FSMState
        for i in range(3):
            _make_thread(
                fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION,
                description=f"t-{i}",
            )
        resp = client.get("/api/threads?limit=2&offset=0")
        body = resp.get_json()
        assert isinstance(body.get("threads"), list)
        assert len(body["threads"]) == 2
        assert body["total"] == 3
        assert body["offset"] == 0
        assert body["limit"] == 2

    def test_pagination_offset_returns_next_page(self, client):
        from work_buddy.threads.enums import FSMState
        for i in range(5):
            _make_thread(
                fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION,
                description=f"t-{i}",
            )
        page1 = client.get("/api/threads?limit=2&offset=0").get_json()
        page2 = client.get("/api/threads?limit=2&offset=2").get_json()
        page3 = client.get("/api/threads?limit=2&offset=4").get_json()
        ids1 = {t["thread_id"] for t in page1["threads"]}
        ids2 = {t["thread_id"] for t in page2["threads"]}
        ids3 = {t["thread_id"] for t in page3["threads"]}
        assert len(ids1) == 2 and len(ids2) == 2 and len(ids3) == 1
        assert not (ids1 & ids2) and not (ids2 & ids3)
        assert page1["total"] == page2["total"] == page3["total"] == 5


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

    def test_run_source_pipeline_is_in_allowlist(self):
        """Allowlist regression: ``run_source_pipeline`` must remain
        in the allowlist; the empty-state CTA depends on it. (Replaces
        the old ``journal_v5_scan`` allowlist entry which was removed
        when the unified pipeline rebuild collapsed per-source scan
        capabilities into one.)"""
        from work_buddy.dashboard.service import (
            _DASHBOARD_RUNNABLE_CAPABILITIES,
        )
        assert "run_source_pipeline" in _DASHBOARD_RUNNABLE_CAPABILITIES


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


# NOTE: ``TestMoveParentEndpoint``, ``TestGroupSiblingsEndpoint``,
# ``TestGroupSubmitEndpoint`` were removed during the unified
# source-pipeline rebuild. The endpoints they covered
# (``/move_parent``, ``/group_siblings``, ``/group_submit``) were
# replaced earlier by ``/move_item``, ``/groups``, ``/approve_all``;
# tests for those new endpoints live in the per-feature test
# modules under ``tests/unit/threads/`` (group ops) and
# ``tests/unit/pipelines/`` (pipeline-level integration).


# ---------------------------------------------------------------------------
# /api/threads/<id>/action_options
# ---------------------------------------------------------------------------


class TestActionOptionsEndpoint:
    """The per-thread action library endpoint backs the inner-thread
    action switcher (a child opened directly has no group grid to
    populate the switcher's options)."""

    def test_returns_source_library_for_journal_thread(self, client):
        t = _make_thread(source="journal_backlog", description="meditation")
        resp = client.get(f"/api/threads/{t.thread_id}/action_options")
        assert resp.status_code == 200
        data = resp.get_json()
        opts = data["action_options"]
        by_name = {o["capability_name"]: o for o in opts}
        assert "journal_route_to_tasks" in by_name
        assert by_name["journal_route_to_tasks"]["cardinality"] == "per_group"

    def test_unknown_source_returns_universal_actions(self, client):
        t = _make_thread(source="unknown_source")
        resp = client.get(f"/api/threads/{t.thread_id}/action_options")
        assert resp.status_code == 200
        names = {o["capability_name"] for o in resp.get_json()["action_options"]}
        # Universal thread actions are always present, even with no
        # registered source pipeline.
        assert "thread_dismiss" in names

    def test_resolves_from_child_own_inciting_summary(self, client):
        """A group child carries its own ``source_pipeline``, so it
        resolves to the real library without a parent lookup."""
        from work_buddy.threads.enums import FSMState
        child = Thread(
            fsm_state=FSMState.AWAITING_CONFIRMATION,
            parent_relationship="decompose",
            inciting_event_summary={"source_pipeline": "journal_backlog"},
        )
        store.insert_thread(child)
        resp = client.get(f"/api/threads/{child.thread_id}/action_options")
        assert resp.status_code == 200
        names = {o["capability_name"] for o in resp.get_json()["action_options"]}
        assert "journal_route_to_tasks" in names

    def test_missing_thread_404(self, client):
        resp = client.get("/api/threads/th-does-not-exist/action_options")
        assert resp.status_code == 404

    def test_carries_param_schema_with_required_flags(self, client):
        """Each action carries its parameter schema so the UI can render
        blank required fields and gate Approve."""
        t = _make_thread(source="journal_backlog")
        resp = client.get(f"/api/threads/{t.thread_id}/action_options")
        by_name = {o["capability_name"]: o
                   for o in resp.get_json()["action_options"]}
        append = by_name["journal_append_to_note"]
        params = {p["name"]: p for p in append["parameters"]}
        assert params["note_path"]["required"] is True
        assert params["bullet_prefix"]["required"] is False


# ---------------------------------------------------------------------------
# Approve folds user action edits into a fresh action_inferred
# ---------------------------------------------------------------------------


class TestApproveFoldsActionEdits:
    """Before Approve commits to execution, the user's filled / switched
    params are written as a fresh action_inferred so the executor reads
    them. Exercised at the fold-in helper (not the full /accept, which
    would fire execution)."""

    def _seed_proposal(self, name, params, irreversibility="low"):
        from work_buddy.threads.enums import FSMState
        t = Thread(fsm_state=FSMState.AWAITING_CONFIRMATION)
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={"payload": {
                "kind": "standard",
                "name": name,
                "parameters": dict(params),
                "irreversibility": irreversibility,
                "regret_potential": "low",
                "risk_amplifier": False,
            }},
        ))
        store.update_thread_state(
            t.thread_id, parent_event_id=store.latest_event_id(t.thread_id),
        )
        return store.get_thread(t.thread_id)

    def test_action_shape_fills_params(self, fresh_threads_db):
        from work_buddy.dashboard import service
        t = self._seed_proposal("journal_append_to_note", {})
        with service.app.test_request_context(json={"action": {
            "capability_name": "journal_append_to_note",
            "parameters": {"note_path": "Areas/Mindfulness/Meditation.md"},
        }}):
            service._apply_action_edits_for_execute(t.thread_id, t)
        payload = service._current_action_payload(t.thread_id)
        assert payload["name"] == "journal_append_to_note"
        assert payload["parameters"]["note_path"] == "Areas/Mindfulness/Meditation.md"

    def test_same_action_preserves_risk(self, fresh_threads_db):
        from work_buddy.dashboard import service
        t = self._seed_proposal("send_email", {}, irreversibility="high")
        with service.app.test_request_context(json={"action": {
            "capability_name": "send_email",
            "parameters": {"to": "a@b.com"},
        }}):
            service._apply_action_edits_for_execute(t.thread_id, t)
        payload = service._current_action_payload(t.thread_id)
        assert payload["irreversibility"] == "high"

    def test_action_overrides_merge_into_params(self, fresh_threads_db):
        from work_buddy.dashboard import service
        t = self._seed_proposal("journal_append_to_note", {"bullet_prefix": "- "})
        with service.app.test_request_context(json={"action_overrides": {
            "act-1": {"note_path": "Y.md"},
        }}):
            service._apply_action_edits_for_execute(t.thread_id, t)
        payload = service._current_action_payload(t.thread_id)
        assert payload["parameters"] == {"bullet_prefix": "- ", "note_path": "Y.md"}

    def test_plain_accept_is_noop(self, fresh_threads_db):
        from work_buddy.dashboard import service
        t = self._seed_proposal("journal_append_to_note", {"note_path": "Z.md"})
        before = store.latest_event_id(t.thread_id)
        with service.app.test_request_context(json={}):
            service._apply_action_edits_for_execute(t.thread_id, t)
        assert store.latest_event_id(t.thread_id) == before
