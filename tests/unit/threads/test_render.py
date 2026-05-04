"""v5 Stage 4.3 — render-data builder for the dashboard."""

from __future__ import annotations

import pytest

from work_buddy.threads import cleanup, render, store
from work_buddy.threads.events import (
    KIND_ACTION_INFERRED,
    KIND_INTENT_INFERRED,
    KIND_LATER,
    ThreadEvent,
)
from work_buddy.threads.models import ContextItem, Thread


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "threads.db"
    monkeypatch.setattr(store, "_db_path", lambda: db)
    cleanup.clear_cleanup_adapters()
    yield db
    cleanup.clear_cleanup_adapters()


# ---------------------------------------------------------------------------
# build_render_data
# ---------------------------------------------------------------------------


class TestBuildRenderData:
    def test_unknown_thread_returns_none(self, fresh_db):
        assert render.build_render_data("th-nonexistent") is None

    def test_minimal_thread_renders(self, fresh_db):
        t = Thread(
            inciting_event_summary={"description": "A new thread"},
        )
        store.insert_thread(t)
        data = render.build_render_data(t.thread_id)
        assert data is not None
        assert data["thread_id"] == t.thread_id
        assert data["title"] == "A new thread"
        assert data["intent"]["text"] == "A new thread"
        assert data["context_items"] == []
        assert data["actions"] == []
        assert data["fsm_state"] == "proposed"
        assert data["sub_thread_count"] == 0
        assert data["can_clean_up"] is False
        assert data["has_been_later"] is False

    def test_intent_event_overrides_inciting(self, fresh_db):
        t = Thread(inciting_event_summary={"description": "fallback"})
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_INTENT_INFERRED,
            actor="agent",
            data={"payload": {"intent": "schedule a call"}, "confidence": 0.9},
        ))
        data = render.build_render_data(t.thread_id)
        assert data["intent"]["text"] == "schedule a call"

    def test_context_items_from_thread(self, fresh_db):
        # Stage 5 v2: render preserves the raw ``ContextItem.id`` so
        # that ``threads.group.move_item`` can target items by stable
        # source-pipeline-assigned ids (Chrome tab id, journal item
        # hash, etc.). ``display_index`` carries the 1-based ordering
        # for frontends that previously relied on ``ci-N`` ordinals.
        t = Thread(
            context_items=(
                ContextItem(id="raw1", source="chrome", type="tab",
                            label="GitHub", payload={}),
                ContextItem(id="raw2", source="vault", type="note",
                            label="ECG paper", payload={}),
            ),
        )
        store.insert_thread(t)
        data = render.build_render_data(t.thread_id)
        assert len(data["context_items"]) == 2
        assert data["context_items"][0]["id"] == "raw1"
        assert data["context_items"][0]["display_index"] == 1
        assert data["context_items"][0]["label"] == "GitHub"
        assert data["context_items"][1]["id"] == "raw2"
        assert data["context_items"][1]["display_index"] == 2

    def test_action_inferred_renders_standard(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={"payload": {
                "kind": "standard",
                "name": "create_calendar_event",
                "parameters": {"title": "Sarah's birthday", "duration": 60},
                "confidence": 0.85,
            }},
        ))
        data = render.build_render_data(t.thread_id)
        assert len(data["actions"]) == 1
        assert data["actions"][0]["kind"] == "standard"
        assert data["actions"][0]["name"] == "create_calendar_event"
        assert "Sarah" in data["actions"][0]["plan_summary"]

    def test_action_inferred_renders_improvised(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={"payload": {
                "kind": "improvised",
                "plan_summary": "Open the doc, summarise, send via Slack.",
                "confidence": 0.6,
            }},
        ))
        data = render.build_render_data(t.thread_id)
        assert data["actions"][0]["kind"] == "improvised"
        assert "Open the doc" in data["actions"][0]["plan_summary"]

    def test_improvised_action_preserves_agent_supplied_name(self, fresh_db):
        """REGRESSION (Wave A — 2026-05-03): improvised actions used
        to lose their agent-supplied name (rendered as ``(improvised)``).
        The agent carefully names actions; the render should preserve
        that and let the frontend show the kind separately."""
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={"payload": {
                "kind": "improvised",
                "name": "Research Claude Code Parallel Local Development",
                "plan_summary": "Investigate parallel dev workflows.",
            }},
        ))
        data = render.build_render_data(t.thread_id)
        assert data["actions"][0]["name"] == \
            "Research Claude Code Parallel Local Development"
        assert data["actions"][0]["kind"] == "improvised"

    def test_improvised_action_falls_back_when_name_missing(self, fresh_db):
        """When the agent omits ``name`` on an improvised action,
        the render uses a kind-specific fallback so the card still
        renders something readable."""
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={"payload": {"kind": "improvised", "plan_summary": "..."}},
        ))
        data = render.build_render_data(t.thread_id)
        assert data["actions"][0]["name"] == "(improvised)"

    def test_action_inferred_renders_suggestion(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={"payload": {
                "kind": "suggestion",
                "text": "Call your mom — it's been 3 weeks.",
                "blocked_on": "relationship judgment",
            }},
        ))
        data = render.build_render_data(t.thread_id)
        assert data["actions"][0]["kind"] == "suggestion"
        assert "mom" in data["actions"][0]["plan_summary"]
        assert data["actions"][0]["blocked_on"] == "relationship judgment"

    def test_render_passes_through_confidence(self, fresh_db):
        """REGRESSION (Wave A): confidence on intent/context/action
        events should flow through to render data so the card can
        display "agent is 92% sure" badges. Previously dropped."""
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_INTENT_INFERRED,
            actor="agent",
            data={
                "payload": {"intent": "schedule a call"},
                "confidence": 0.92,
            },
        ))
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={
                "payload": {"kind": "improvised", "name": "do x"},
                "confidence": 0.7,
            },
        ))
        data = render.build_render_data(t.thread_id)
        assert data["intent"]["confidence"] == 0.92
        assert data["actions"][0]["confidence"] == 0.7

    def test_render_passes_through_action_risk_metadata(self, fresh_db):
        """REGRESSION (Wave A): risk metadata declared on the action
        proposal (irreversibility / regret_potential / risk_amplifier)
        was previously dropped. Without it, the consent card can't
        render an urgency pill, and the user has no risk signal."""
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={"payload": {
                "kind": "improvised",
                "name": "delete all email drafts",
                "irreversibility": "high",
                "regret_potential": "high",
                "risk_amplifier": True,
            }},
        ))
        data = render.build_render_data(t.thread_id)
        action = data["actions"][0]
        assert action["irreversibility"] == "high"
        assert action["regret_potential"] == "high"
        assert action["risk_amplifier"] is True
        # Top-level risk_highlight derived from all actions.
        assert data["risk_highlight"] == "high"

    def test_risk_highlight_low_for_safe_actions(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={"payload": {
                "kind": "improvised",
                "name": "log a note",
                "irreversibility": "low",
                "regret_potential": "low",
                "risk_amplifier": False,
            }},
        ))
        data = render.build_render_data(t.thread_id)
        assert data["risk_highlight"] == "low"

    def test_risk_highlight_none_when_no_risk_metadata(self, fresh_db):
        """Pure suggestion or older threads without risk metadata
        get None — frontend just doesn't show the pill."""
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_ACTION_INFERRED,
            actor="agent",
            data={"payload": {"kind": "suggestion", "text": "..."}},
        ))
        data = render.build_render_data(t.thread_id)
        assert data["risk_highlight"] is None

    def test_sub_thread_state_counts_aggregated(self, fresh_db):
        """Wave I: parent's render data includes per-state counts
        of sub-threads so the card can show "5 done · 4 awaiting
        consent" badges per UX.md §8.1."""
        from work_buddy.threads.enums import FSMState
        parent = Thread(
            inciting_event_summary={"description": "parent"},
        )
        store.insert_thread(parent)
        # Three children in different states
        for state in (
            FSMState.AWAITING_CONFIRMATION,
            FSMState.AWAITING_CONFIRMATION,
            FSMState.DONE,
        ):
            c = Thread(
                parent_id=parent.thread_id,
                fsm_state=state,
                inciting_event_summary={"description": "child"},
            )
            store.insert_thread(c)
        data = render.build_render_data(parent.thread_id)
        counts = data["sub_thread_state_counts"]
        assert counts.get("awaiting_confirmation") == 2
        assert counts.get("done") == 1
        assert data["sub_thread_count"] == 3

    def test_auto_advance_trail_present(self, fresh_db):
        """The auto_advance_decision events are surfaced on render
        data so the consent card can show 'agent auto-advanced
        through intent + context'."""
        from work_buddy.threads.events import KIND_AUTO_ADVANCE_DECISION
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_AUTO_ADVANCE_DECISION,
            actor="fsm_engine",
            data={
                "target": "intent",
                "advance": True,
                "confidence": 0.92,
                "chosen_state": "awaiting_inference",
            },
        ))
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_AUTO_ADVANCE_DECISION,
            actor="fsm_engine",
            data={
                "target": "context",
                "advance": True,
                "confidence": 0.85,
                "chosen_state": "awaiting_inference",
            },
        ))
        data = render.build_render_data(t.thread_id)
        trail = data["auto_advance_trail"]
        assert len(trail) == 2
        assert trail[0]["target"] == "intent"
        assert trail[0]["advance"] is True
        assert trail[0]["confidence"] == 0.92
        assert trail[1]["target"] == "context"

    def test_display_mode_actionable_for_wait_state(self, fresh_db):
        """Phase 4: render data carries display_mode so the frontend
        knows whether to show the full card (actionable), a muted
        in-flight indicator (mid_process), or read-only (terminal)."""
        from work_buddy.threads.enums import FSMState
        t = Thread(fsm_state=FSMState.AWAITING_INTENT_CONFIRMATION)
        store.insert_thread(t)
        data = render.build_render_data(t.thread_id)
        assert data["display_mode"] == "actionable"

    def test_display_mode_mid_process_for_inferring(self, fresh_db):
        from work_buddy.threads.enums import FSMState
        t = Thread(fsm_state=FSMState.INFERRING_INTENT)
        store.insert_thread(t)
        data = render.build_render_data(t.thread_id)
        assert data["display_mode"] == "mid_process"

    def test_display_mode_terminal_for_done(self, fresh_db):
        from work_buddy.threads.enums import FSMState
        t = Thread(fsm_state=FSMState.DONE)
        store.insert_thread(t)
        data = render.build_render_data(t.thread_id)
        assert data["display_mode"] == "terminal"

    def test_can_clean_up_reflects_adapter(self, fresh_db):
        from work_buddy.threads.cleanup import CleanupAdapter, CleanupResult
        cleanup.register_cleanup_adapter(CleanupAdapter(
            source="my_source",
            can_clean_up=lambda t: True,
            cleanup=lambda t: CleanupResult(success=True),
        ))
        t = Thread(inciting_event_summary={"source": "my_source"})
        store.insert_thread(t)
        data = render.build_render_data(t.thread_id)
        assert data["can_clean_up"] is True

    def test_has_been_later_reads_event_log(self, fresh_db):
        t = Thread()
        store.insert_thread(t)
        store.append_event(ThreadEvent(
            thread_id=t.thread_id,
            kind=KIND_LATER,
            actor="user",
            data={"hours": 6},
        ))
        data = render.build_render_data(t.thread_id)
        assert data["has_been_later"] is True

    def test_sub_thread_count(self, fresh_db):
        parent = Thread()
        store.insert_thread(parent)
        for i in range(3):
            store.insert_thread(Thread(parent_id=parent.thread_id))
        data = render.build_render_data(parent.thread_id)
        assert data["sub_thread_count"] == 3


# ---------------------------------------------------------------------------
# list_render_data
# ---------------------------------------------------------------------------


class TestListRenderData:
    def test_top_level_returns_only_root_threads(self, fresh_db):
        a = Thread()
        b = Thread()
        store.insert_thread(a)
        store.insert_thread(b)
        # Add a child to b
        store.insert_thread(Thread(parent_id=b.thread_id))
        out = render.list_render_data()
        ids = {t["thread_id"] for t in out}
        assert a.thread_id in ids
        assert b.thread_id in ids
        assert len(out) == 2  # child not included

    def test_sub_listing_filters_by_parent(self, fresh_db):
        p = Thread()
        store.insert_thread(p)
        c1 = Thread(parent_id=p.thread_id)
        c2 = Thread(parent_id=p.thread_id)
        store.insert_thread(c1)
        store.insert_thread(c2)
        out = render.list_render_data(parent_id=p.thread_id)
        ids = {t["thread_id"] for t in out}
        assert ids == {c1.thread_id, c2.thread_id}

    def test_resurface_future_filtered_by_default(self, fresh_db):
        from datetime import datetime, timedelta, timezone
        t = Thread()
        store.insert_thread(t)
        future = (
            datetime.now(timezone.utc) + timedelta(hours=12)
        ).isoformat()
        store.update_thread_state(t.thread_id, resurface_at=future)
        out = render.list_render_data()
        # By default, future-resurface excluded
        assert all(td["thread_id"] != t.thread_id for td in out)
        # With include_resurface_future, present
        out2 = render.list_render_data(include_resurface_future=True)
        assert any(td["thread_id"] == t.thread_id for td in out2)
