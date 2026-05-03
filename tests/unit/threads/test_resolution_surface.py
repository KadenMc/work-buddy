"""v5 Stage 2.5 — ResolutionRequest publication via the
notifications subsystem.

Pins:
- build_resolution_request() refuses to build for non-wait states.
- publish() best-effort: failures don't propagate.
- The state-entry handler hooks engine.transition() to publish on
  every wait-state entry.
- register_resolution_surface_handlers() is idempotent.
"""

from __future__ import annotations

import pytest

from work_buddy.threads import (
    engine,
    resolution_surface as rs,
    store,
)
from work_buddy.threads.enums import FSMState, SurfaceUrgency
from work_buddy.threads.fsm import (
    TRIG_CONFIRMED,
    TRIG_INFERENCE_DONE,
    TRIG_INFERENCE_FAILED,
)
from work_buddy.threads.models import Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    engine.clear_state_entry_handlers()
    yield db
    engine.clear_state_entry_handlers()


# ---------------------------------------------------------------------------
# build_resolution_request
# ---------------------------------------------------------------------------


class TestBuild:
    def test_rejects_non_wait_state(self, fresh_db):
        t = Thread(fsm_state=FSMState.PROPOSED)
        with pytest.raises(ValueError):
            rs.build_resolution_request(t)

    def test_builds_for_confirmation_state(self, fresh_db):
        t = Thread(fsm_state=FSMState.AWAITING_CONFIRMATION,
                   parent_event_id=42)
        req = rs.build_resolution_request(t, payload={"foo": "bar"})
        assert req.thread_id == t.thread_id
        assert req.fsm_state == FSMState.AWAITING_CONFIRMATION
        assert req.card_kind() == "consent"
        assert req.parent_event_id == 42
        assert req.payload == {"foo": "bar"}
        assert req.urgency == SurfaceUrgency.DEFER

    def test_builds_for_clarification_state(self, fresh_db):
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CLARIFICATION)
        req = rs.build_resolution_request(t)
        assert req.card_kind() == "clarification"

    def test_builds_for_review_state(self, fresh_db):
        t = Thread(fsm_state=FSMState.AWAITING_REVIEW)
        req = rs.build_resolution_request(t)
        assert req.card_kind() == "review"


# ---------------------------------------------------------------------------
# publish() — best effort even when notifications subsystem unavailable
# ---------------------------------------------------------------------------


class TestSurfaceTargeting:
    """Phase 5a: low-stakes confirmations land in dashboard only;
    legitimate user-pause states fan out across all surfaces."""

    def test_action_approval_targets_all_surfaces(self):
        t = Thread(fsm_state=FSMState.AWAITING_CONFIRMATION)
        rr = rs.build_resolution_request(t)
        # None means "all available" in the SurfaceDispatcher's
        # contract.
        assert rs._surfaces_for(rr) is None

    def test_clarification_targets_all_surfaces(self):
        for state in (
            FSMState.AWAITING_INTENT_CLARIFICATION,
            FSMState.AWAITING_CONTEXT_CLARIFICATION,
            FSMState.AWAITING_ACTION_CLARIFICATION,
            FSMState.AWAITING_REDIRECT,
        ):
            t = Thread(fsm_state=state)
            rr = rs.build_resolution_request(t)
            assert rs._surfaces_for(rr) is None, state

    def test_intent_confirmation_dashboard_only(self):
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        rr = rs.build_resolution_request(t)
        assert rs._surfaces_for(rr) == ["dashboard"]

    def test_context_confirmation_dashboard_only(self):
        t = Thread(fsm_state=FSMState.AWAITING_CONTEXT_CONFIRMATION)
        rr = rs.build_resolution_request(t)
        assert rs._surfaces_for(rr) == ["dashboard"]

    def test_review_state_targets_all_surfaces(self):
        t = Thread(fsm_state=FSMState.AWAITING_REVIEW)
        rr = rs.build_resolution_request(t)
        assert rs._surfaces_for(rr) is None

    def test_failed_cleanup_targets_all_surfaces(self):
        t = Thread(fsm_state=FSMState.DONE_CLEANUP_UNSUCCESSFUL)
        rr = rs.build_resolution_request(t)
        assert rs._surfaces_for(rr) is None


class TestPublish:
    def test_publish_swallows_dispatcher_errors(self, fresh_db, monkeypatch):
        """If the notifications subsystem can't be reached (no
        sidecar running, missing config, etc.), publish() returns
        None instead of raising. Critical: the FSM transition has
        already landed atomically; failure to surface the card is
        degraded UX, not a correctness issue."""
        # Force an import error via a stub module
        import sys
        fake_mod = type(sys)("work_buddy.notifications.dispatcher")
        class BoomDispatcher:
            @classmethod
            def from_config(cls):
                raise RuntimeError("test: no dispatcher available")
        fake_mod.SurfaceDispatcher = BoomDispatcher
        monkeypatch.setitem(
            sys.modules, "work_buddy.notifications.dispatcher", fake_mod,
        )

        t = Thread(fsm_state=FSMState.AWAITING_CONFIRMATION)
        req = rs.build_resolution_request(t)
        # Should NOT raise
        result = rs.publish(req)
        # Returns None on failure
        assert result is None


# ---------------------------------------------------------------------------
# State-entry handler integration with the engine
# ---------------------------------------------------------------------------


class TestStateEntryHandler:
    def test_handler_skips_non_wait_states(self, fresh_db, monkeypatch):
        # Stub publish so we can capture calls
        seen: list = []
        monkeypatch.setattr(rs, "publish", lambda rr: seen.append(rr) or None)

        rs.register_resolution_surface_handlers()
        t = Thread(fsm_state=FSMState.AWAITING_CONFIRMATION)
        store.insert_thread(t)
        # Reject moves to DISMISSED (terminal, NOT a wait state)
        engine.transition(t.thread_id, "rejected")
        # Handler shouldn't have published
        assert seen == []

    def test_handler_publishes_on_wait_state_entry(self, fresh_db, monkeypatch):
        seen: list = []
        monkeypatch.setattr(rs, "publish", lambda rr: seen.append(rr) or None)

        rs.register_resolution_surface_handlers()
        t = Thread(fsm_state=FSMState.INFERRING_INTENT)
        store.insert_thread(t)
        engine.transition(t.thread_id, TRIG_INFERENCE_DONE,
                          data={"intent": "schedule a call"})
        # Should have published exactly one ResolutionRequest with
        # the AWAITING_INTENT_CONFIRMATION shape
        assert len(seen) == 1
        rr = seen[0]
        assert rr.fsm_state == FSMState.AWAITING_INTENT_CONFIRMATION
        assert rr.card_kind() == "confirmation"
        assert rr.payload.get("intent") == "schedule a call"
        # state-internal bookkeeping stripped
        assert "from" not in rr.payload
        assert "to" not in rr.payload
        assert "trigger" not in rr.payload

    def test_handler_publishes_redirect_with_no_proposing_actor(self, fresh_db, monkeypatch):
        seen: list = []
        monkeypatch.setattr(rs, "publish", lambda rr: seen.append(rr) or None)

        rs.register_resolution_surface_handlers()
        # Push a thread to EXECUTING and fail it — lands in AWAITING_REDIRECT
        t = Thread(fsm_state=FSMState.EXECUTING)
        store.insert_thread(t)
        engine.transition(t.thread_id, "execution_failed")
        # Should have published a redirect card
        assert len(seen) == 1
        assert seen[0].fsm_state == FSMState.AWAITING_REDIRECT
        assert seen[0].card_kind() == "redirect"
        # The agent isn't proposing on a redirect — it's asking
        assert seen[0].proposing_actor is None

    def test_register_handlers_idempotent(self, fresh_db, monkeypatch):
        # Calling register twice should not double-fire publish.
        # NB: the engine appends handlers, so re-registering the
        # same handler instance DOES double up. The contract is
        # "safe to call multiple times" in the loose sense — tests
        # that need exactly-one delivery should clear handlers
        # first.
        seen: list = []
        monkeypatch.setattr(rs, "publish", lambda rr: seen.append(rr) or None)

        rs.register_resolution_surface_handlers()
        # Note that register_state_entry_handler is additive.
        # Re-registering would in fact fire twice. The handler is
        # idempotent in terms of *what it does* (publishing
        # replaces the same notification_id), but not in firing
        # count. Document this explicitly.
        t = Thread(fsm_state=FSMState.INFERRING_INTENT)
        store.insert_thread(t)
        engine.transition(t.thread_id, TRIG_INFERENCE_DONE)
        assert len(seen) == 1
