"""State-entry handler that runs cleanup adapters.

When the FSM transitions a Thread into CLEANING_UP, this handler
runs the registered adapter and fires the appropriate result
trigger (cleanup_succeeded or cleanup_failed) — which in turn
transitions the Thread to a terminal-or-retry state.

UX.md §6.4 + §6.5 are the spec.

The handler is registered by bootstrap_threads() at sidecar startup.
"""

from __future__ import annotations

import logging

from work_buddy.threads import cleanup, engine, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    KIND_CLEANUP_FAILED,
    KIND_SOURCE_CLEANED_UP,
    ThreadEvent,
)
from work_buddy.threads.fsm import (
    TRIG_CLEANUP_FAILED,
    TRIG_CLEANUP_SUCCEEDED,
)

logger = logging.getLogger(__name__)


def cleanup_state_entry_handler(transition_result) -> None:
    """Engine state-entry handler for CLEANING_UP.

    Reads the latest Thread snapshot, invokes the registered
    cleanup adapter (if any), records the result event, and fires
    the matching trigger to advance the FSM.

    If no adapter is registered (shouldn't happen — the UI gates
    Clean Up to threads with applicable adapters — but defensive),
    transitions to DONE_CLEANUP_UNSUCCESSFUL with a diagnostic.
    """
    if transition_result.next_state != FSMState.CLEANING_UP:
        return

    thread_id = transition_result.thread_id
    thread = store.get_thread(thread_id)
    if thread is None:
        logger.warning(
            "Thread %s vanished between transition and cleanup", thread_id,
        )
        return

    result = cleanup.perform_cleanup(thread)

    # Record the cleanup outcome on the Thread log
    fresh_parent = store.latest_event_id(thread_id)
    event_kind = (
        KIND_SOURCE_CLEANED_UP if result.success else KIND_CLEANUP_FAILED
    )
    try:
        store.append_event(ThreadEvent(
            thread_id=thread_id,
            kind=event_kind,
            actor="agent",
            data={
                "success": result.success,
                "detail": result.detail,
                "source_already_gone": result.source_already_gone,
            },
            parent_event_id=fresh_parent,
        ))
    except Exception as e:
        logger.warning(
            "Failed to record cleanup event for %s: %s", thread_id, e,
        )

    # Fire the appropriate FSM trigger
    trig = TRIG_CLEANUP_SUCCEEDED if result.success else TRIG_CLEANUP_FAILED
    fresh_parent = store.latest_event_id(thread_id)
    try:
        engine.transition(
            thread_id, trig,
            data={
                "detail": result.detail,
                "source_already_gone": result.source_already_gone,
            },
            parent_event_id=fresh_parent,
            fire_side_effects=True,
        )
    except engine.InvalidTransition:
        logger.warning(
            "Cleanup result trigger %s rejected for %s in state %s",
            trig, thread_id,
            store.get_thread(thread_id).fsm_state.value if thread_id else "?",
        )


def register_cleanup_runner() -> None:
    """Wire ``cleanup_state_entry_handler`` to FSMState.CLEANING_UP.

    Stage 4.4 bootstrap calls this. Tests may call it explicitly.
    Idempotent at the engine level — re-registering appends another
    handler that does the same work, but the inner perform_cleanup
    call is a no-op the second time (status already advanced).
    """
    engine.register_state_entry_handler(
        FSMState.CLEANING_UP, cleanup_state_entry_handler,
    )
