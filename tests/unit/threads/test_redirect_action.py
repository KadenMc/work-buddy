"""Per-action redirect with scoped re-inference.

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

    def test_empty_feedback_allowed(self, fresh_db, client):
        """A bare redirect (no typed feedback) is valid — a "try this
        again", with the auto swap-context supplying the meaning. It
        records an event with empty feedback and transitions."""
        t = _thread_with_pending_action({"kind": "standard", "name": "task_create"})
        resp = client.post(
            f"/api/threads/{t.thread_id}/redirect_action",
            data=json.dumps({"feedback": "   "}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.get_json()
        events = store.list_events(t.thread_id, kinds=[KIND_ACTION_REDIRECTED])
        assert len(events) == 1
        assert events[0].data["feedback"] == ""
        assert (
            store.get_thread(t.thread_id).fsm_state == FSMState.AWAITING_INFERENCE
        )

    def test_seeds_and_target_action_recorded(self, fresh_db, client):
        """The switched-to action + partial fields the user filled ride
        along on the redirect event for the inference runner to seed."""
        t = _thread_with_pending_action({
            "kind": "standard", "name": "journal_append_to_note",
        })
        resp = client.post(
            f"/api/threads/{t.thread_id}/redirect_action",
            data=json.dumps({
                "feedback": "",
                "params": {"note_path": "Areas/Mindfulness/Meditation.md"},
                "target_action": "journal_append_to_note",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.get_json()
        ev = store.list_events(t.thread_id, kinds=[KIND_ACTION_REDIRECTED])[0]
        assert ev.data["seed_params"] == {
            "note_path": "Areas/Mindfulness/Meditation.md",
        }
        assert ev.data["target_action"] == "journal_append_to_note"

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

    def test_target_action_and_seeds_surface(self, fresh_db):
        """A switch-redirect (target action + partial fields, no typed
        feedback) surfaces the target action and the kept values so the
        agent proposes that action and fills only the gaps."""
        t = _thread_with_pending_action({
            "kind": "standard", "name": "journal_append_to_note",
        })
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_REDIRECTED,
            actor=ACTOR_USER,
            data={
                "feedback": "",
                "seed_params": {"note_path": "Areas/M.md"},
                "target_action": "journal_route_to_tasks",
            },
        ))
        block = self._build(t)
        assert block != ""
        assert "journal_route_to_tasks" in block
        assert "Areas/M.md" in block
        assert "Keep these parameter values" in block

    def test_seeds_without_feedback_not_skipped(self, fresh_db):
        t = _thread_with_pending_action({"kind": "standard", "name": "x"})
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_REDIRECTED,
            actor=ACTOR_USER,
            data={"feedback": "", "seed_params": {"k": "v"}},
        ))
        assert self._build(t) != ""


class TestActionCatalogRequiredMarking:
    """The action catalog injected into the inference prompt marks
    required params so the model fills them."""

    def test_required_params_marked_with_star(self):
        from work_buddy.threads.bootstrap import _maybe_format_action_catalog
        from work_buddy.threads.enums import InferenceTarget
        from work_buddy.threads.inference import TARGETS
        schema = TARGETS[InferenceTarget.ACTION].output_schema
        block = _maybe_format_action_catalog(schema)
        # journal_append_to_note declares note_path as required.
        assert "note_path*" in block
        assert "REQUIRED parameter" in block


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
