"""ResolutionRequest publisher — bridges the FSM to the dashboard.

Stage 2.5 deliverable. When the FSM transitions a Thread into a
wait state (any ``awaiting_*``), this module publishes a
ResolutionRequest as a workflow-view Notification carrying the
Stage-1.9 Resolution Surface payload (``type='resolution_request'``).

The frontend (``script_resolution_surface_v5.py``) renders the
matching card kind based on payload.card_kind.

Wiring
------

``register_resolution_surface_handlers()`` registers a state-entry
handler with ``work_buddy.threads.engine`` for every wait state.
Stage 2.9 calls this during sidecar bootstrap. Tests can call it
explicitly.

Why notifications, not consent
------------------------------

DESIGN.md §7.3 says the Resolution Request "flows through the
existing consent subsystem." The existing consent subsystem
(``work_buddy/consent.py``) is shaped around capability-call
gating decorators, not generic typed messages. Retrofitting it
to carry v5 ResolutionRequest payloads is Stage 4 surface-redesign
work; in the meantime, the notifications subsystem already has a
generic custom_template + workflow-view renderer mechanism that
fits cleanly. The user never sees the difference; the routing is
internal.

When the consent subsystem is retrofitted, this module's
publisher swaps backend with no change to Thread-level callers.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Optional

from work_buddy.threads import store
from work_buddy.threads.enums import FSMState, SurfaceUrgency
from work_buddy.threads.engine import TransitionResult, register_state_entry_handler
from work_buddy.threads.models import ResolutionRequest, Thread

logger = logging.getLogger(__name__)


_PUBLISHED_VIEW_PREFIX = "resolution-"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def build_resolution_request(
    thread: Thread,
    *,
    proposing_actor: Optional[str] = "agent",
    urgency: Optional[SurfaceUrgency] = None,
    payload: Optional[dict[str, Any]] = None,
    deadline: Optional[str] = None,
) -> ResolutionRequest:
    """Create a ResolutionRequest from a Thread's current state.

    Pulls the thread's current parent_event_id as the optimistic-lock
    target so the user's eventual response can be verified.
    """
    if not thread.fsm_state.is_wait_state:
        raise ValueError(
            f"Thread {thread.thread_id} is in {thread.fsm_state.value!r}, "
            f"which is not a wait state — cannot build a ResolutionRequest.",
        )
    return ResolutionRequest(
        thread_id=thread.thread_id,
        fsm_state=thread.fsm_state,
        proposing_actor=proposing_actor,
        urgency=urgency or SurfaceUrgency.DEFER,
        payload=payload or {},
        deadline=deadline,
        parent_event_id=thread.parent_event_id,
    )


# ---------------------------------------------------------------------------
# Notification dispatch
# ---------------------------------------------------------------------------
#
# Mirrors the conversation_chat / capability_consent pattern:
# create a Notification with a custom_template, dispatch via
# SurfaceDispatcher, the dashboard's poll loop spawns a workflow-
# view, and our v5 frontend renderer (registered in Stage 1.9)
# picks it up.
# ---------------------------------------------------------------------------


def publish(rr: ResolutionRequest) -> Optional[str]:
    """Publish a ResolutionRequest as a workflow-view Notification.

    Returns the workflow-view ID (``resolution-<thread_id>``) on
    success, or None if the notification subsystem isn't running
    (best-effort — the FSM transition has already landed
    atomically; failure to surface a card is degraded UX, not a
    correctness issue).
    """
    view_id = f"{_PUBLISHED_VIEW_PREFIX}{rr.thread_id}"

    try:
        from work_buddy.notifications.dispatcher import SurfaceDispatcher
        from work_buddy.notifications.models import Notification, ResponseType
        from work_buddy.notifications.store import (
            create_notification as _create_notif,
            mark_delivered as _mark_delivered,
        )

        # Title hints at what the user must do; body is the proposal
        # summary if available.
        title = _title_for(rr)
        body = _body_for(rr)

        notif = Notification(
            notification_id=view_id,
            title=title,
            body=body,
            response_type=ResponseType.NONE.value,  # custom card has its own affordances
            custom_template={
                "type": "resolution_request",
                "thread_id": rr.thread_id,
                "fsm_state": rr.fsm_state.value,
                "card_kind": rr.card_kind(),
                "proposing_actor": rr.proposing_actor,
                "urgency": rr.urgency.value,
                "payload": rr.payload,
                "deadline": rr.deadline,
                "parent_event_id": rr.parent_event_id,
            },
            expandable=True,
        )
        created = _create_notif(notif)
        dispatcher = SurfaceDispatcher.from_config()
        dispatcher.deliver(created, mark_delivered_fn=_mark_delivered)
        return view_id
    except Exception as e:
        logger.warning(
            "publish() failed for thread %s in state %s: %s — "
            "the FSM transition has landed; the surface card will "
            "be missing until the next state change re-publishes.",
            rr.thread_id, rr.fsm_state.value, e,
        )
        return None


def _title_for(rr: ResolutionRequest) -> str:
    kind = rr.card_kind()
    label = {
        "confirmation": "Confirm",
        "clarification": "Clarification needed",
        "consent": "Approve action",
        "review": "Review result",
        "redirect": "Redirect needed",
    }.get(kind, "Resolution")
    return f"{label}: {rr.thread_id}"


def _body_for(rr: ResolutionRequest) -> str:
    # Try common payload conventions
    for key in ("intent", "summary", "description", "plan_summary",
                "rationale"):
        v = rr.payload.get(key)
        if isinstance(v, str) and v:
            return v[:240]
    return rr.fsm_state.value.replace("_", " ")


# ---------------------------------------------------------------------------
# State-entry handler
# ---------------------------------------------------------------------------


def _state_entry_handler(result: TransitionResult) -> None:
    """Engine state-entry handler for any wait state.

    Reads the latest Thread snapshot (the engine just updated the
    cache) and publishes a ResolutionRequest. Idempotent on the
    notification ID — re-entering the same state replaces the
    existing card.
    """
    if not result.next_state.is_wait_state:
        return
    thread = store.get_thread(result.thread_id)
    if thread is None:
        logger.warning(
            "Thread %s vanished between transition and publish",
            result.thread_id,
        )
        return
    # Carry through any payload data from the transition (action
    # proposals, intent guesses, etc.) into the Resolution Request.
    payload = dict(result.data)
    # State-internal bookkeeping (from/to/trigger) isn't useful to
    # the user-facing card; strip it.
    for k in ("from", "to", "trigger"):
        payload.pop(k, None)

    proposing_actor = "agent"
    if result.next_state == FSMState.AWAITING_REDIRECT:
        # On a failed execution, the agent isn't proposing
        # anything — it's asking the user to redirect.
        proposing_actor = None

    rr = build_resolution_request(
        thread,
        proposing_actor=proposing_actor,
        payload=payload,
    )
    publish(rr)


def register_resolution_surface_handlers() -> None:
    """Register the state-entry handler for every wait state.

    Stage 2.9 (sidecar bootstrap) calls this. Tests may call it
    explicitly. Idempotent — safe to call multiple times in tests
    that didn't clear handlers.
    """
    for state in FSMState:
        if state.is_wait_state:
            register_state_entry_handler(state, _state_entry_handler)
