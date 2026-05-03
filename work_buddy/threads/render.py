"""Render-data builder for the v5 Threads dashboard.

Converts a Thread + its event log into the JSON shape the
confirmation card consumes. UX.md §4 + per-section data shapes.

Stage 4.3 deliverable. The builder is pure (no FSM mutations);
the endpoints layer on top of this.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from work_buddy.threads import cleanup, store
from work_buddy.threads.events import (
    KIND_ACTION_INFERRED,
    KIND_CONTEXT_INFERRED,
    KIND_INCITING_EVENT,
    KIND_INTENT_INFERRED,
    KIND_LATER,
)
from work_buddy.threads.models import Thread

logger = logging.getLogger(__name__)


def build_render_data(thread_id: str) -> Optional[dict[str, Any]]:
    """Return the JSON shape consumed by ``renderConfirmationCard``.

    Returns None if the Thread doesn't exist.
    """
    thread = store.get_thread(thread_id)
    if thread is None:
        return None

    events = store.list_events(thread_id)

    # Inciting summary → for title fallback
    inciting = thread.inciting_event_summary or {}

    # Pull the latest *_inferred events for each target
    latest_intent = _latest(events, KIND_INTENT_INFERRED)
    latest_context = _latest(events, KIND_CONTEXT_INFERRED)
    latest_action = _latest(events, KIND_ACTION_INFERRED)

    intent_text = ""
    if latest_intent is not None:
        payload = latest_intent.data.get("payload") or {}
        intent_text = payload.get("intent") or ""
    if not intent_text:
        # Fallback to inciting summary
        intent_text = (
            inciting.get("description")
            or inciting.get("summary")
            or ""
        )

    # Context items: Thread.context_items first; then any
    # context_inferred events that added to the list. For 4.3 we
    # use thread.context_items as source of truth — Stage 4.5+
    # consolidates the two.
    context_items = []
    for i, ci in enumerate(thread.context_items, start=1):
        context_items.append({
            "id": f"ci-{i}",
            "label": ci.label or ci.id,
            "source": ci.source,
            "type": ci.type,
            "payload": ci.payload,
        })

    # Actions: from the latest action_inferred event's payload
    actions = []
    if latest_action is not None:
        payload = latest_action.data.get("payload") or {}
        # Action proposals can carry one or many actions. The v5
        # convention from DESIGN.md §10 is one ActionProposal at a
        # time; we render whatever's there.
        kind = payload.get("kind", "standard")
        if kind == "standard":
            actions.append(_attach_context_status({
                "id": f"act-{latest_action.id}",
                "name": payload.get("name", "(unnamed)"),
                "kind": "standard",
                "parameters": payload.get("parameters") or {},
                "plan_summary": _summarise_action(payload),
                "required_contexts": payload.get("required_contexts") or [],
                "intrinsic_amplifiers": payload.get("intrinsic_amplifiers") or {},
            }))
        elif kind == "improvised":
            actions.append(_attach_context_status({
                "id": f"act-{latest_action.id}",
                "name": "(improvised)",
                "kind": "improvised",
                "parameters": {},
                "plan_summary": payload.get("plan_summary") or "",
                "required_contexts": payload.get("required_contexts") or [],
                "intrinsic_amplifiers": payload.get("intrinsic_amplifiers") or {},
            }))
        elif kind == "suggestion":
            actions.append(_attach_context_status({
                "id": f"act-{latest_action.id}",
                "name": "(suggestion)",
                "kind": "suggestion",
                "parameters": {},
                "plan_summary": payload.get("text") or "",
                "required_contexts": [],
                "intrinsic_amplifiers": {},
            }))

    # Urgency — derive from inciting summary or default to defer
    urgency = inciting.get("urgency", "defer")

    # Title — derive from inciting + intent
    title = inciting.get("title") or inciting.get("description") or intent_text or thread.thread_id

    # Sub-thread count
    sub_count = len(store.list_threads(parent_id=thread_id))

    # Derive card kind from FSM state. UX.md §4.2.
    card_kind = _card_kind_for(thread.fsm_state.value)

    # For redirect cards, surface the failure context.
    failure_context: Optional[dict[str, Any]] = None
    if thread.fsm_state.value == "awaiting_redirect":
        failure_context = _latest_failure_context(events)

    # For review cards, surface the execution result.
    review_context: Optional[dict[str, Any]] = None
    if thread.fsm_state.value == "awaiting_review":
        review_context = _latest_review_context(events)

    # For cleanup-failed cards, surface the failure detail.
    cleanup_failure: Optional[dict[str, Any]] = None
    if thread.fsm_state.value == "done_cleanup_unsuccessful":
        cleanup_failure = _latest_cleanup_failure(events)

    # display_mode tells the frontend how to render the thread:
    # - "actionable": full card with affordances (Accept / Edit /
    #   Redirect). The default for any wait state.
    # - "mid_process": muted card with a "currently inferring..."
    #   status line, no action buttons. Surfaced only via the
    #   "Show mid-process" toggle (Phase 4 of the autonomy plan).
    # - "terminal": done/dismissed/handed-off — read-only.
    if thread.fsm_state.is_terminal:
        display_mode = "terminal"
    elif thread.fsm_state.is_wait_state:
        display_mode = "actionable"
    else:
        display_mode = "mid_process"

    return {
        "thread_id": thread.thread_id,
        "parent_id": thread.parent_id,
        "subtype": thread.subtype,
        "title": title,
        "urgency": urgency,
        "fsm_state": thread.fsm_state.value,
        "card_kind": card_kind,
        "display_mode": display_mode,
        "intent": {"text": intent_text, "editable": True},
        "context_items": context_items,
        "actions": actions,
        "namespace_tags": list(inciting.get("namespace_tags") or []),
        "can_clean_up": cleanup.can_clean_up(thread),
        "sub_thread_count": sub_count,
        "has_been_later": _has_been_later(events),
        "resurface_at": getattr(thread, "resurface_at", None),
        "parent_event_id": thread.parent_event_id,
        "failure_context": failure_context,
        "review_context": review_context,
        "cleanup_failure": cleanup_failure,
    }


def list_render_data(
    *,
    parent_id: Optional[str] = None,
    include_resurface_future: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return a list of render-data shapes for a top-level or
    sub-thread listing.

    For top-level (parent_id=None), filters out future-resurface
    Threads unless ``include_resurface_future=True``.
    """
    threads = store.list_threads(parent_id=parent_id)
    # store.list_threads with parent_id=None returns ALL threads;
    # for "top-level only" we filter post-query.
    if parent_id is None:
        threads = [t for t in threads if t.parent_id is None]
    out: list[dict[str, Any]] = []
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    for t in threads:
        if (parent_id is None
                and not include_resurface_future
                and getattr(t, "resurface_at", None)
                and t.resurface_at > now):
            continue
        rd = build_render_data(t.thread_id)
        if rd is not None:
            out.append(rd)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _latest(events, kind):
    """Return the most-recent event of ``kind``, or None."""
    for e in reversed(events):
        if e.kind == kind:
            return e
    return None


def _has_been_later(events) -> bool:
    for e in events:
        if e.kind == KIND_LATER:
            return True
    return False


_CARD_KIND_BY_STATE: dict[str, str] = {
    # Confirmation
    "awaiting_intent_confirmation": "confirmation",
    "awaiting_context_confirmation": "confirmation",
    # Consent (action gate — same shape, different emphasis)
    "awaiting_confirmation": "consent",
    # Clarification
    "awaiting_intent_clarification": "clarification",
    "awaiting_context_clarification": "clarification",
    "awaiting_action_clarification": "clarification",
    # Post-execution
    "awaiting_review": "review",
    # Failure / redirect
    "awaiting_redirect": "redirect",
    # Cleanup failure (UX.md §6.5 — retry/accept-failure UI)
    "done_cleanup_unsuccessful": "cleanup_failure",
}


def _card_kind_for(fsm_state: str) -> str:
    return _CARD_KIND_BY_STATE.get(fsm_state, "confirmation")


def _latest_failure_context(events) -> Optional[dict[str, Any]]:
    """Pull the latest execution_failed-style data for redirect cards."""
    for e in reversed(events):
        if e.kind in ("execution_finished", "step_failed"):
            data = e.data or {}
            if data.get("success") is False or data.get("status") == "failed":
                return {
                    "error": data.get("error") or data.get("detail"),
                    "step": data.get("step"),
                    "summary": data.get("summary"),
                }
    return None


def _latest_review_context(events) -> Optional[dict[str, Any]]:
    """Pull the latest execution_finished payload for review cards."""
    for e in reversed(events):
        if e.kind == "execution_finished":
            data = e.data or {}
            return {
                "status": data.get("status") or "completed",
                "output": data.get("output"),
                "summary": data.get("summary") or data.get("detail"),
                "run_id": data.get("run_id"),
            }
    return None


def _latest_cleanup_failure(events) -> Optional[dict[str, Any]]:
    """Pull the latest cleanup_failed for the retry/accept card."""
    for e in reversed(events):
        if e.kind == "cleanup_failed":
            data = e.data or {}
            return {
                "detail": data.get("detail"),
                "source_already_gone": data.get("source_already_gone", False),
            }
    return None


def _attach_context_status(action: dict[str, Any]) -> dict[str, Any]:
    """Add a `context_statuses` field to an action dict.

    Stage 4.11: each required-context token gets an availability
    status object so the UI can render the per-action indicator.
    """
    try:
        from work_buddy.threads.context_status import context_statuses
        action["context_statuses"] = context_statuses(
            action.get("required_contexts") or [],
        )
    except Exception:
        action["context_statuses"] = []
    # Derived: any_unavailable = True if any required context's
    # status is unavailable (excluding user_only — those route to
    # the user not the agent).
    any_blocking = any(
        not s.get("available") and s.get("kind") != "user_only"
        for s in action["context_statuses"]
    )
    action["context_blocked"] = any_blocking
    return action


def _summarise_action(payload: dict[str, Any]) -> str:
    """Brief one-line summary of an action proposal — title-or-first-param."""
    if "plan_summary" in payload and payload["plan_summary"]:
        return str(payload["plan_summary"])
    params = payload.get("parameters") or {}
    if not params:
        return ""
    # Prefer common high-yield keys
    for key in ("title", "subject", "description", "name"):
        if key in params:
            return f"{params[key]}"
    # Fallback: first key:value
    first = next(iter(params))
    v = params[first]
    if isinstance(v, (str, int, float, bool)):
        return f"{first}: {v}"
    return f"{first}: {json.dumps(v)[:60]}"
