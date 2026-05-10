"""Render-data builder for the Threads dashboard.

Converts a Thread + its event log into the JSON shape the
confirmation card consumes. UX.md §4 + per-section data shapes.

The builder is pure (no FSM mutations);
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
    intent_confidence: Optional[float] = None
    if latest_intent is not None:
        payload = latest_intent.data.get("payload") or {}
        intent_text = payload.get("intent") or ""
        intent_confidence = latest_intent.data.get("confidence")
    if not intent_text:
        # Fallback to inciting summary
        intent_text = (
            inciting.get("description")
            or inciting.get("summary")
            or ""
        )

    # Context confidence — surfaced so the user can see how
    # certain the agent was. (When confidence < the policy's
    # floor, we wouldn't have auto-advanced past this state, so
    # the value is informative for "why am I being asked to
    # confirm this?")
    context_confidence: Optional[float] = None
    if latest_context is not None:
        context_confidence = latest_context.data.get("confidence")

    # Context items: Thread.context_items first; then any
    # context_inferred events that added to the list. For 4.3 we
    # use thread.context_items as source of truth — Stage 4.5+
    # consolidates the two.
    #
    # We MUST preserve ``ContextItem.id`` (the canonical source-
    # pipeline-assigned id like ``journal_t_926fa6`` or a Chrome
    # tab id) because the ``threads.group.move_item`` operation
    # targets items by their stable id. Synthetic display-only
    # ``ci-{i}`` indexes break the move endpoint.
    context_items = []
    for i, ci in enumerate(thread.context_items, start=1):
        context_items.append({
            "id": ci.id,
            "display_index": i,  # 1-based display order
            "label": ci.label or ci.id,
            "source": ci.source,
            "type": ci.type,
            "payload": ci.payload,
        })

    # Actions: from the latest action_inferred event's payload.
    # Confidence + risk metadata are read off the event so the
    # consent card can render the right urgency pill + risk
    # disclosure without re-querying the autonomy_branch resolver.
    actions = []
    if latest_action is not None:
        payload = latest_action.data.get("payload") or {}
        action_confidence = latest_action.data.get("confidence")
        # Risk metadata: the agent declares irreversibility /
        # regret_potential / risk_amplifier on the proposal payload
        # (improvised) OR they come from the Standard Action template's
        # intrinsic_amplifiers (the template-level mapping). We
        # surface BOTH on the render dict so the frontend can show
        # whichever applies.
        risk = {
            "irreversibility": payload.get("irreversibility"),
            "regret_potential": payload.get("regret_potential"),
            "risk_amplifier": payload.get("risk_amplifier", False),
        }
        intrinsic = payload.get("intrinsic_amplifiers") or {}

        # Action proposals can carry one or many actions. The
        # convention is one ActionProposal at a
        # time; we render whatever's there.
        kind = payload.get("kind", "standard")
        # Action display name: agent-supplied name takes precedence
        # for ALL kinds. Pre-Wave-A bug: improvised/suggestion paths
        # hardcoded the kind label as the name, dropping the agent's
        # carefully-chosen name. Now we use the name and let the
        # frontend show the kind as a separate badge.
        action_name = payload.get("name") or _kind_fallback_name(kind)
        # plan_summary differs slightly per kind: standard uses the
        # generic summariser (which prefers parameters.title etc.),
        # suggestion lives in payload['text'], improvised has its
        # own plan_summary.
        if kind == "suggestion":
            plan_summary = payload.get("text") or payload.get("plan_summary") or ""
        elif kind == "improvised":
            plan_summary = payload.get("plan_summary") or ""
        else:
            plan_summary = _summarise_action(payload)
        actions.append(_attach_context_status({
            "id": f"act-{latest_action.id}",
            "name": action_name,
            "kind": kind,
            "parameters": payload.get("parameters") or {},
            "plan_summary": plan_summary,
            "rationale": payload.get("rationale") or "",
            "blocked_on": payload.get("blocked_on") or "",
            "required_contexts": payload.get("required_contexts") or [],
            "intrinsic_amplifiers": intrinsic,
            "irreversibility": risk["irreversibility"],
            "regret_potential": risk["regret_potential"],
            "risk_amplifier": bool(risk["risk_amplifier"]),
            "confidence": action_confidence,
            "model_used": latest_action.data.get("model_used"),
        }))

    # Compute sub-threads once; reused both for the singular-pattern
    # hoist (below) and for sub_thread_count / state-aggregation badges
    # (further below). `list_threads(parent_id=...)` orders by
    # `order_index ASC`, so iteration here is in spawn order.
    sub_threads = store.list_threads(parent_id=thread_id)

    # Singular-pattern hoist: when this thread is a `parent_relationship='singular'`
    # umbrella (created by `pipelines/inline.py:_spawn_inline_umbrella` for inline
    # captures whose verdict produced 2+ records), surface the children's actions
    # inline on the parent's card. The umbrella itself has no `action_inferred`
    # event — by spawn-time design — so `actions` is empty at this point. Each
    # child thread carries exactly one action. We hoist them onto this parent's
    # rendered actions array so the user sees ONE thread with N proposals
    # instead of an empty umbrella + N detached child cards. See `threads/grouping`
    # (singular pattern) for the architectural rationale.
    if (
        getattr(thread, "parent_relationship", "decompose") == "singular"
        and not actions  # Umbrella shouldn't have its own action; defensive.
    ):
        for _child in sub_threads:
            _child_render = build_render_data(_child.thread_id)
            if _child_render is None:
                continue
            _child_state = _per_action_state_from_fsm(_child.fsm_state.value)
            _settled = _child_state in ("done", "rejected", "failed")
            for _child_action in (_child_render.get("actions") or []):
                _hoisted = dict(_child_action)
                _hoisted["host_thread_id"] = _child.thread_id
                _hoisted["state"] = _child_state
                _hoisted["settled"] = _settled
                actions.append(_hoisted)
        # Stable partition: pending first (in child-spawn order, which
        # is the order `list_threads` returned them), settled last.
        # Per the user's UX request: actions don't reorder once they
        # appear (muscle memory holds).
        actions = (
            [a for a in actions if not a.get("settled")]
            + [a for a in actions if a.get("settled")]
        )

    # Urgency — derive from inciting summary or default to defer
    urgency = inciting.get("urgency", "defer")

    # Title — prefer an explicit inciting.title (set by parent
    # spawners e.g. journal_scan, chrome_scrape) since that
    # carries distinguishing context like the date. Sub-threads
    # and journal-line threads don't set inciting.title — for
    # those, the agent's inferred intent text is cleaner than the
    # raw description.
    #
    # User-feedback fix #4 (2026-05-03): intent should win for
    # individual threads. Followup: but when inciting.title is
    # explicitly set (parents only), it's MORE distinguishing than
    # the often-generic intent ("Process daily notes").
    # So: explicit title > intent > description > thread_id.
    title = (
        inciting.get("title")
        or intent_text
        or inciting.get("description")
        or thread.thread_id
    )

    # Sub-thread count + per-state aggregation. UX.md §8.1
    # specifies the parent's detail view should show
    # "5 done • 4 awaiting consent • 2 awaiting clarification"
    # at the top of the sub-thread list. We aggregate here so the
    # frontend can render the badges without an extra API call.
    # `sub_threads` was already computed above for the singular-hoist
    # path; reused here.
    sub_count = len(sub_threads)
    sub_thread_state_counts: dict[str, int] = {}
    for st in sub_threads:
        key = st.fsm_state.value
        sub_thread_state_counts[key] = sub_thread_state_counts.get(key, 0) + 1

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

    # Auto-advance trail — the audit events that record the
    # autonomy resolver's decisions. Surfaced as a small
    # breadcrumb on the consent card so the user can see "the
    # agent powered through these intent + context decisions on
    # its own" at a glance.
    auto_advance_trail = _auto_advance_trail(events)

    # Latest activity timestamp — the timestamp of the most-recent
    # event. Used by the frontend to render relative time
    # ("just now", "5m ago"). Falls back to thread.updated_at.
    latest_activity = events[-1].timestamp if events else None
    if latest_activity is None:
        latest_activity = getattr(thread, "updated_at", None)

    # Creation timestamp — the timestamp of the earliest event on the
    # log (typically the inciting_event). Used by the frontend to
    # disambiguate cards that share the same title (e.g. multiple
    # "Chrome triage" umbrellas spawned across a day).
    created_at = events[0].timestamp if events else None

    # Risk highlight — true if the action's risk metadata exceeds
    # a "review-worthy" bar. Used by the consent card to apply a
    # color-coded urgency pill.
    risk_highlight = _risk_highlight(actions)

    return {
        "thread_id": thread.thread_id,
        "parent_id": thread.parent_id,
        "subtype": thread.subtype,
        "title": title,
        "urgency": urgency,
        "fsm_state": thread.fsm_state.value,
        "card_kind": card_kind,
        "display_mode": display_mode,
        "intent": {
            "text": intent_text,
            "editable": True,
            "confidence": intent_confidence,
        },
        "context": {
            "confidence": context_confidence,
        },
        "context_items": context_items,
        "actions": actions,
        "risk_highlight": risk_highlight,
        "auto_advance_trail": auto_advance_trail,
        "latest_activity": latest_activity,
        "created_at": created_at,
        "namespace_tags": list(inciting.get("namespace_tags") or []),
        "can_clean_up": cleanup.can_clean_up(thread),
        "sub_thread_count": sub_count,
        "sub_thread_state_counts": sub_thread_state_counts,
        # relationship discriminator + sibling-scope id. The
        # frontend uses these to choose between the standard sub-thread
        # mini-card list (decompose) and the multi-column group view
        # (group). Always emitted so the dashboard can rely on the
        # field being present.
        "parent_relationship": getattr(thread, "parent_relationship", "decompose"),
        "originating_scrape_id": getattr(thread, "originating_scrape_id", None),
        "has_been_later": _has_been_later(events),
        "resurface_at": getattr(thread, "resurface_at", None),
        "parent_event_id": thread.parent_event_id,
        "failure_context": failure_context,
        "review_context": review_context,
        "cleanup_failure": cleanup_failure,
    }


def _per_action_state_from_fsm(fsm_state: str) -> str:
    """Map a child Thread's FSM state to a per-action-state label.

    Used by the singular-pattern hoist (when a parent_relationship='singular'
    umbrella surfaces its children's actions inline on its own card). Each
    child thread is the host for exactly one action; the action's effective
    state is a derived view of the child's fsm_state.

    Returns one of: ``pending`` / ``executing`` / ``done`` / ``rejected`` /
    ``failed``. The frontend uses this to gray-out settled actions and pick
    a status badge.

    Note: ``rejected`` vs ``failed`` distinction is approximate. ``DISMISSED``
    state is reported as ``rejected`` here for v1 simplicity. A more precise
    derivation would inspect the event log for ``execution_finished(success=False)``
    vs a direct rejection, but the user-visible difference is minor (both
    are settled-and-gray) and the cost-of-precision is meaningful.
    """
    state = fsm_state.lower() if isinstance(fsm_state, str) else ""
    if state == "awaiting_confirmation":
        return "pending"
    if state == "executing":
        return "executing"
    if state == "done":
        return "done"
    if state == "dismissed":
        return "rejected"
    # Mid-process states (inferring_*, awaiting_intent_*, etc.) are
    # still "pending" from the user's POV — not yet ready for approval,
    # but also not settled.
    return "pending"


def _kind_fallback_name(kind: str) -> str:
    """Used when the agent omitted ``name`` on an action proposal.

    Pre-Wave-A behavior was to use this as the canonical name,
    which clobbered agent-supplied names. Now it's a true fallback:
    only used when ``payload.get('name')`` is missing or empty.
    """
    return {
        "standard": "(unnamed standard action)",
        "improvised": "(improvised)",
        "suggestion": "(suggestion)",
    }.get(kind, "(unknown)")


def _auto_advance_trail(events) -> list[dict[str, Any]]:
    """Pull the autonomy-resolver decisions in chronological order.

    Returns a compact list shape:

        [{"target": "intent", "advance": True, "confidence": 0.92},
         {"target": "context", "advance": True, "confidence": 0.85}]

    Empty when no auto_advance_decision events have landed (e.g.
    pre-autonomy threads or threads under hands_off policy).
    """
    from work_buddy.threads.events import KIND_AUTO_ADVANCE_DECISION
    trail = []
    for e in events:
        if e.kind != KIND_AUTO_ADVANCE_DECISION:
            continue
        d = e.data or {}
        trail.append({
            "target": d.get("target"),
            "advance": bool(d.get("advance")),
            "confidence": d.get("confidence"),
            "chosen_state": d.get("chosen_state"),
        })
    return trail


_RISK_RANK = {"low": 0, "medium": 1, "high": 2, None: -1}


def _risk_highlight(actions: list[dict[str, Any]]) -> Optional[str]:
    """Decide what risk pill to show on the consent card.

    Returns one of ``None``, ``'low'``, ``'medium'``, ``'high'``.

    The highlight is derived from the action's declared risk
    metadata + the intrinsic amplifiers from the Standard Action
    registry (when the action is standard). If any of
    irreversibility / regret_potential is high → high. If either
    is medium OR risk_amplifier is True → medium. If both low → low.
    None when no risk metadata is available (e.g. pure suggestion).
    """
    if not actions:
        return None
    levels: list[int] = []
    for a in actions:
        irrev = (a.get("irreversibility") or
                 (a.get("intrinsic_amplifiers") or {}).get("irreversibility"))
        regret = (a.get("regret_potential") or
                  (a.get("intrinsic_amplifiers") or {}).get("regret_potential"))
        amp = bool(a.get("risk_amplifier"))
        rank = max(_RISK_RANK.get(irrev, -1), _RISK_RANK.get(regret, -1))
        if amp:
            rank = max(rank, _RISK_RANK["medium"])
        levels.append(rank)
    top = max(levels) if levels else -1
    if top < 0:
        return None
    return {0: "low", 1: "medium", 2: "high"}.get(top)


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

    each required-context token gets an availability
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
