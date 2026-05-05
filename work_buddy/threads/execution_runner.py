"""State-entry handler that runs a Thread's chosen action.

When the FSM transitions a Thread into :data:`FSMState.EXECUTING`,
this handler reads the latest ``action_inferred`` event off the
Thread, looks up the named capability in the MCP registry, binds
parameters (auto-filling Chrome-specific ``tab_ids`` from the
Thread's context items), invokes it, records ``execution_started``
+ ``execution_finished`` events, and fires
:data:`TRIG_EXECUTION_DONE` (or :data:`TRIG_EXECUTION_FAILED`) to
advance the FSM.

Mirrors the shape of :mod:`work_buddy.threads.cleanup_runner` —
both are state-entry handlers that perform a side-effect and fire
a follow-up trigger.

Bootstrap registers this handler in :func:`bootstrap_v5`.
"""

from __future__ import annotations

import logging
from typing import Any

from work_buddy.threads import engine, store
from work_buddy.threads.enums import FSMState
from work_buddy.threads.events import (
    ACTOR_AGENT,
    KIND_ACTION_INFERRED,
    KIND_EXECUTION_FINISHED,
    KIND_EXECUTION_STARTED,
    ThreadEvent,
)
from work_buddy.threads.fsm import (
    TRIG_EXECUTION_DONE,
    TRIG_EXECUTION_FAILED,
)

logger = logging.getLogger(__name__)


def execution_state_entry_handler(transition_result) -> None:
    """Engine state-entry handler for :data:`FSMState.EXECUTING`.

    Reads the latest non-cleared ``action_inferred`` event, dispatches
    its capability via the MCP registry, then fires the matching
    completion trigger. Failures land an ``execution_finished`` event
    flagged ``success=False`` and fire :data:`TRIG_EXECUTION_FAILED`.
    """
    if transition_result.next_state != FSMState.EXECUTING:
        return

    thread_id = transition_result.thread_id
    thread = store.get_thread(thread_id)
    if thread is None:
        logger.warning(
            "execution_runner: thread %s vanished between transition "
            "and execute", thread_id,
        )
        return

    proposal = _latest_action_proposal(thread_id)
    if proposal is None:
        _record_failure(
            thread_id,
            error="no action_inferred event found",
            capability=None,
        )
        return

    capability_name = proposal.get("name") or ""
    raw_parameters = dict(proposal.get("parameters") or {})

    # Bind dynamic parameters that depend on the thread's runtime
    # state (e.g. tab_ids pulled from context_items for chrome_tab_*
    # actions). Static parameters set on the proposal at refine-time
    # win — we only fill in what wasn't already provided.
    bound = _bind_runtime_parameters(
        capability_name=capability_name,
        thread=thread,
        provided=raw_parameters,
    )

    # Audit start.
    store.append_event(ThreadEvent(
        thread_id=thread_id,
        kind=KIND_EXECUTION_STARTED,
        actor=ACTOR_AGENT,
        data={
            "capability_name": capability_name,
            "parameters": bound,
        },
        parent_event_id=store.latest_event_id(thread_id),
    ))
    store.update_thread_state(
        thread_id,
        parent_event_id=store.latest_event_id(thread_id),
    )

    # Dispatch.
    success, result, error = _invoke_capability(
        capability_name=capability_name, parameters=bound,
    )

    # Audit finish (always — both success + failure paths).
    store.append_event(ThreadEvent(
        thread_id=thread_id,
        kind=KIND_EXECUTION_FINISHED,
        actor=ACTOR_AGENT,
        data={
            "capability_name": capability_name,
            "success": success,
            "result": _truncate_for_log(result),
            "error": error,
        },
        parent_event_id=store.latest_event_id(thread_id),
    ))
    store.update_thread_state(
        thread_id,
        parent_event_id=store.latest_event_id(thread_id),
    )

    # Advance the FSM. parent_event_id pinned to latest — engine.transition
    # checks optimistic-lock against it.
    trig = TRIG_EXECUTION_DONE if success else TRIG_EXECUTION_FAILED
    try:
        engine.transition(
            thread_id, trig,
            data={
                "success": success,
                "error": error,
                "capability_name": capability_name,
            },
            parent_event_id=store.latest_event_id(thread_id),
            fire_side_effects=True,
        )
    except engine.InvalidTransition:
        logger.warning(
            "execution_runner: trigger %s rejected for %s in state %s",
            trig, thread_id,
            store.get_thread(thread_id).fsm_state.value
            if store.get_thread(thread_id)
            else "?",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _latest_action_proposal(thread_id: str) -> dict[str, Any] | None:
    """Walk the Thread's events newest-first; return the latest non-
    cleared ``action_inferred`` event's payload."""
    events = store.list_events(thread_id=thread_id)
    for e in reversed(events):
        if e.kind != KIND_ACTION_INFERRED:
            continue
        if e.data.get("cleared"):
            continue
        payload = e.data.get("payload") or {}
        if not payload.get("name"):
            continue
        return payload
    return None


def _bind_runtime_parameters(
    *,
    capability_name: str,
    thread,
    provided: dict[str, Any],
) -> dict[str, Any]:
    """Fill in capability parameters that depend on thread runtime
    state. Static params from the proposal win.

    Currently covers the Chrome-action capabilities (which all need
    ``tab_ids`` extracted from the Thread's context items). Other
    capabilities pass through unchanged.
    """
    out = dict(provided)

    if capability_name in (
        "chrome_tab_close", "chrome_tab_group", "chrome_tab_move",
    ):
        if "tab_ids" not in out:
            out["tab_ids"] = _collect_tab_ids(thread)

    if capability_name in (
        "chrome_route_to_tasks", "chrome_route_to_umbrella_task",
    ):
        if "thread_id" not in out:
            out["thread_id"] = thread.thread_id

    return out


def _collect_tab_ids(thread) -> list[int]:
    """Extract integer tab_ids from a Thread's context items."""
    out: list[int] = []
    for ci in thread.context_items or ():
        payload = (
            ci.payload if hasattr(ci, "payload")
            else (ci.get("payload") if isinstance(ci, dict) else None)
        )
        if not isinstance(payload, dict):
            continue
        tab_id = payload.get("tab_id")
        if isinstance(tab_id, int):
            out.append(tab_id)
    return out


def _invoke_capability(
    *, capability_name: str, parameters: dict[str, Any],
) -> tuple[bool, Any, str | None]:
    """Look up the capability in the MCP registry and call it.

    Returns ``(success, result, error_msg)``. The capability's
    callable is called with ``**parameters``; any exception is caught
    and surfaced as a failure.
    """
    try:
        from work_buddy.mcp_server.registry import get_registry
    except Exception as e:
        return (False, None, f"registry import failed: {e}")

    registry = get_registry()
    entry = registry.get(capability_name)
    if entry is None:
        return (
            False, None,
            f"capability {capability_name!r} not registered",
        )
    callable_ = getattr(entry, "callable", None)
    if not callable(callable_):
        return (
            False, None,
            f"capability {capability_name!r} has no callable",
        )

    try:
        result = callable_(**parameters)
    except TypeError as e:
        return (False, None, f"parameter mismatch: {e}")
    except Exception as e:  # noqa: BLE001 — capability errors are surfaced
        logger.exception(
            "execution_runner: capability %s raised", capability_name,
        )
        return (False, None, f"{type(e).__name__}: {e}")

    success = _result_succeeded(result)
    return (success, result, None if success else _result_error(result))


def _result_succeeded(result: Any) -> bool:
    """Heuristic: treat dict results as successful unless they declare
    otherwise. Non-dict results count as success (we have no signal).
    """
    if isinstance(result, dict):
        if result.get("error"):
            return False
        if "success" in result:
            return bool(result["success"])
    return True


def _result_error(result: Any) -> str | None:
    if isinstance(result, dict):
        err = result.get("error")
        if err:
            return str(err)
    return None


def _truncate_for_log(value: Any, max_chars: int = 1000) -> Any:
    """Keep the audit event small. Dicts shrink to a one-line repr."""
    if value is None:
        return None
    s = repr(value)
    if len(s) <= max_chars:
        return value
    return {"_truncated": True, "_len": len(s), "_preview": s[:max_chars]}


def register_execution_runner() -> None:
    """Wire :func:`execution_state_entry_handler` to
    :data:`FSMState.EXECUTING`. Idempotent at the engine level —
    re-registering appends another handler that does the same work,
    but the inner ``execute`` call is guarded by the FSM advancing
    out of EXECUTING after the first invocation, so the second is
    a near-no-op.
    """
    engine.register_state_entry_handler(
        FSMState.EXECUTING, execution_state_entry_handler,
    )
