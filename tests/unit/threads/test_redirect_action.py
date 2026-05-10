"""Phase 5 — per-action redirect with scoped re-inference.

Verifies:
- The dashboard's ``/api/threads/<id>/redirect_action`` endpoint
  records a ``KIND_ACTION_REDIRECTED`` event with feedback, transitions
  the thread back to ``AWAITING_INFERENCE`` with ``target='action'``,
  and returns the prior ``action_inferred`` event id as
  ``superseded_event_id``.
- The inference runner's redirect-feedback block (in
  ``bootstrap._build_redirect_feedback_block``) surfaces fresh
  unresolved redirect feedback into the LLM prompt; resolved
  feedback (a newer action_inferred event has already landed) is
  skipped.
- ``render`` continues to pick the latest ``action_inferred`` event
  as the active proposal — the prior one stays in history.
"""

from __future__ import annotations

import json

import pytest

from work_buddy.threads import autonomy, engine, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    ACTOR_AGENT,
    ACTOR_USER,
    KIND_ACTION_INFERRED,
    KIND_ACTION_REDIRECTED,
    ThreadEvent,
)
from work_buddy.threads.models import Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    engine.clear_state_entry_handlers()
    yield db
    engine.clear_state_entry_handlers()


def _thread_with_pending_action(payload: dict) -> Thread:
    """Spawn a thread in AWAITING_CONFIRMATION carrying one
    ``action_inferred`` event with the given payload."""
    t = Thread(
        autonomy_policy=autonomy.PLAN_THEN_REVIEW,
        fsm_state=FSMState.AWAITING_CONFIRMATION,
    )
    store.insert_thread(t)
    store.append_event(ThreadEvent(
        thread_id=t.thread_id,
        kind=KIND_ACTION_INFERRED,
        actor=ACTOR_AGENT,
        data={
            "target": "action",
            "payload": payload,
            "confidence": 0.7,
        },
    ))
    return t


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class TestRedirectActionEndpoint:
    """``POST /api/threads/<id>/redirect_action``."""

    @pytest.fixture
    def client(self, fresh_db):
        # Disable real LLM hookup; the redirect endpoint should not
        # trigger an actual inference call in a unit test (the state-
        # entry handler is wired by bootstrap which we don't fire).
        from work_buddy.dashboard.service import app
        app.testing = True
        return app.test_client()

    def test_records_feedback_event_and_transitions(self, fresh_db, client):
        t = _thread_with_pending_action({
            "kind": "standard", "name": "calendar_event_suggested",
            "parameters": {"title": "Sarah's birthday", "datetime": "2026-05-11T18:00"},
        })

        resp = client.post(
            f"/api/threads/{t.thread_id}/redirect_action",
            data=json.dumps({"feedback": "reminder a week earlier"}),
            content_type="application/json",
        )
        body = resp.get_json()
        assert resp.status_code == 200, body
        assert body["ok"] is True
        assert body["prev_state"] == "awaiting_confirmation"
        assert body["next_state"] == "awaiting_inference"
        assert body["superseded_event_id"] is not None

        # action_redirected event lives in the log with the feedback
        events = store.list_events(t.thread_id, kinds=[KIND_ACTION_REDIRECTED])
        assert len(events) == 1
        assert events[0].data["feedback"] == "reminder a week earlier"
        assert events[0].data["superseded_event_id"] == body["superseded_event_id"]
        assert events[0].actor == ACTOR_USER

    def test_empty_feedback_rejected(self, fresh_db, client):
        t = _thread_with_pending_action({"kind": "standard", "name": "task_create"})
        resp = client.post(
            f"/api/threads/{t.thread_id}/redirect_action",
            data=json.dumps({"feedback": "   "}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "feedback is required" in (resp.get_json() or {}).get("error", "")
        # No event recorded, state unchanged
        assert not store.list_events(t.thread_id, kinds=[KIND_ACTION_REDIRECTED])
        assert (
            store.get_thread(t.thread_id).fsm_state == FSMState.AWAITING_CONFIRMATION
        )

    def test_no_optimistic_lock_conflict_after_feedback_event(
        self, fresh_db, client,
    ):
        """The endpoint appends KIND_ACTION_REDIRECTED, then transitions
        the FSM. ``append_event`` writes the event row but does NOT bump
        the thread's cached ``parent_event_id``; the transition's
        optimistic-lock target must be refreshed explicitly via
        ``store.latest_event_id``. Without that refresh, the transition
        compares its stale cached id against the freshly-landed feedback
        event and raises OptimisticLockConflict — surfaced to the user
        as a 500 with a "latest event N, expected M; re-read and retry"
        message. Pin the working endpoint shape so a future refactor
        can't reintroduce the conflict.
        """
        t = _thread_with_pending_action({
            "kind": "standard", "name": "task_create",
            "parameters": {"task_text": "thing"},
        })
        # Simulate a side effect having bumped the latest event without
        # touching the thread's cached parent_event_id (mirrors what
        # happens when an upstream cron / surface publish lands an event
        # between the dashboard fetching the thread and the user clicking
        # Redirect).
        from work_buddy.threads.events import KIND_AUTO_ADVANCE_DECISION
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_AUTO_ADVANCE_DECISION,
            actor=ACTOR_AGENT,
            data={"unrelated": "side effect"},
        ))

        resp = client.post(
            f"/api/threads/{t.thread_id}/redirect_action",
            data=json.dumps({"feedback": "do something else"}),
            content_type="application/json",
        )
        body = resp.get_json()
        assert resp.status_code == 200, body
        assert body["next_state"] == "awaiting_inference"

    def test_missing_thread_404(self, fresh_db, client):
        resp = client.post(
            "/api/threads/th-nonexistent/redirect_action",
            data=json.dumps({"feedback": "doesn't matter"}),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_invalid_state_400(self, fresh_db, client):
        """Trying to redirect from EXECUTING (the action is already
        running) is rejected by the FSM, surfaces as 400."""
        t = _thread_with_pending_action({"kind": "standard", "name": "task_create"})
        store.update_thread_state(t.thread_id, fsm_state=FSMState.EXECUTING.value)
        resp = client.post(
            f"/api/threads/{t.thread_id}/redirect_action",
            data=json.dumps({"feedback": "change it"}),
            content_type="application/json",
        )
        # FSM rejects TRIG_REDIRECTED from EXECUTING; the feedback
        # event was already recorded before the engine.transition call
        # raises — that's acceptable audit (the user clicked redirect)
        # but the state didn't actually change.
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# _build_redirect_feedback_block
# ---------------------------------------------------------------------------


class TestRedirectFeedbackBlock:
    """Verify the helper that surfaces redirect feedback into the
    LLM user message."""

    def _build(self, thread):
        from work_buddy.threads.bootstrap import _build_redirect_feedback_block
        return _build_redirect_feedback_block(thread)

    def test_no_events_returns_empty(self, fresh_db):
        t = Thread(fsm_state=FSMState.AWAITING_INFERENCE)
        store.insert_thread(t)
        assert self._build(t) == ""

    def test_fresh_redirect_surfaces_feedback(self, fresh_db):
        t = _thread_with_pending_action({
            "kind": "standard", "name": "task_create",
            "parameters": {"task_text": "old"},
        })
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_REDIRECTED,
            actor=ACTOR_USER,
            data={"feedback": "make it urgent"},
        ))
        block = self._build(t)
        assert "User redirect" in block
        assert "make it urgent" in block
        # prior proposal summary surfaced so the LLM doesn't repeat it
        assert "task_create" in block

    def test_resolved_redirect_skipped(self, fresh_db):
        """When a newer ``action_inferred`` event lands AFTER the
        redirect (meaning the inference already happened), the
        feedback has already been addressed — don't re-inject it on
        a subsequent re-inference round."""
        t = _thread_with_pending_action({
            "kind": "standard", "name": "old_action",
        })
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_REDIRECTED,
            actor=ACTOR_USER,
            data={"feedback": "do something else"},
        ))
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor=ACTOR_AGENT,
            data={
                "target": "action",
                "payload": {"kind": "standard", "name": "new_action"},
                "confidence": 0.8,
            },
        ))
        assert self._build(t) == ""

    def test_empty_feedback_string_skipped(self, fresh_db):
        t = _thread_with_pending_action({"kind": "standard", "name": "x"})
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_REDIRECTED,
            actor=ACTOR_USER,
            data={"feedback": ""},
        ))
        assert self._build(t) == ""


# ---------------------------------------------------------------------------
# Render: latest action_inferred surfaces; prior stays in history
# ---------------------------------------------------------------------------


class TestRenderAfterRedirect:
    def test_latest_action_inferred_surfaces(self, fresh_db):
        """After a redirect cycle (old action_inferred → redirect →
        new action_inferred), render.build_render_data must surface
        the newest action_inferred as the active proposal."""
        t = _thread_with_pending_action({
            "kind": "standard", "name": "old_action",
            "parameters": {"x": 1},
        })
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_REDIRECTED,
            actor=ACTOR_USER,
            data={"feedback": "wrong, try again"},
        ))
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor=ACTOR_AGENT,
            data={
                "target": "action",
                "payload": {
                    "kind": "standard", "name": "new_action",
                    "parameters": {"x": 2},
                },
                "confidence": 0.85,
            },
        ))
        from work_buddy.threads.render import build_render_data
        data = build_render_data(t.thread_id)
        assert data is not None
        actions = data.get("actions") or []
        assert len(actions) == 1
        assert actions[0]["name"] == "new_action"
        assert actions[0]["parameters"] == {"x": 2}
