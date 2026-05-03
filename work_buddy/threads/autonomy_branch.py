"""Autonomy-gated branch resolvers — the runtime that reads
``AutonomyPolicy`` and decides whether to surface a confirmation card
or auto-advance.

DESIGN.md §11 documented the policy axes; Stage 2 shipped the data
model + composition + override-down validation; this module is the
missing piece that **reads** those axes when the FSM transitions out
of an INFERRING_* state.

Wiring
------

The three resolvers below are registered by extending the engine's
default branch resolver to recognize three new labels:

    intent_review_or_advance    (INFERRING_INTENT  + TRIG_INFERENCE_DONE)
    context_review_or_advance   (INFERRING_CONTEXT + TRIG_INFERENCE_DONE)
    action_review_or_execute    (INFERRING_ACTION  + TRIG_INFERENCE_DONE)

When auto-advance applies, the FSM **never enters** the corresponding
``AWAITING_*_CONFIRMATION`` state — its state-entry handlers (notification
publish, etc.) never fire. The thread skips silently to the next
inference target (or to EXECUTING for the action resolver).

Decision rule (uniform across the three resolvers, with action-specific
extras applied only by ``resolve_action_branch``):

  auto-advance iff:
    1. The would-be wait state is in ``policy.auto_advance_states``.
    2. ``data['confidence'] >= policy.inference_confidence_floor``.
    3. The proposal's event kind is NOT in
       ``policy.consent_required_kinds``.
    4. (action only) action.irreversibility ≤
       ``policy.irreversibility_threshold``.
    5. (action only) action.regret_potential ≤
       ``policy.regret_potential_threshold``.
    6. (action only) NOT (``policy.pause_on_risk_amplifier``
       AND action.has_risk_amplifier).
    7. (action only) action.kind in
       ``policy.allowed_action_kinds``.

Provenance: every auto-advance decision (positive or negative) is
recorded as an ``auto_advance_decision`` event so the user can audit
"what did the agent decide on its own?" via the thread detail view
or the future mid-process visibility toggle.

Conservative defaults: when the proposal payload omits a risk field
(action.irreversibility, etc.), the resolver treats it as the most
conservative possible value — i.e., as if the agent declared it as
``high`` — which forces user review. This is the right shape for an
LLM that may forget to declare its own risk; the cost of erring
toward "ask the user" is small relative to erring toward
"silently mutate the world."
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from work_buddy.threads import autonomy, store
from work_buddy.threads.enums import (
    ActionKind,
    FSMState,
)
from work_buddy.threads.events import (
    ACTOR_FSM_ENGINE,
    KIND_AUTO_ADVANCE_DECISION,
    ThreadEvent,
)
from work_buddy.threads.models import AutonomyPolicy, Thread

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Risk rank order
# ---------------------------------------------------------------------------


_RISK_RANK = {"low": 0, "medium": 1, "high": 2}


def _rank(value: Optional[str]) -> int:
    """Rank a risk value. Unknown / missing → most conservative (high)."""
    if value is None:
        return _RISK_RANK["high"]
    return _RISK_RANK.get(str(value).lower(), _RISK_RANK["high"])


# ---------------------------------------------------------------------------
# Policy access
# ---------------------------------------------------------------------------


def _policy_for(thread: Thread, *, conn=None) -> AutonomyPolicy:
    """Resolve the effective policy for a thread, walking parents."""
    return autonomy.effective_policy_for(thread, conn=conn)


# ---------------------------------------------------------------------------
# Decision predicates (pure functions — no I/O)
# ---------------------------------------------------------------------------


def _confidence_meets_floor(
    data: dict[str, Any], policy: AutonomyPolicy,
) -> bool:
    confidence = data.get("confidence")
    if confidence is None:
        return False
    try:
        return float(confidence) >= float(policy.inference_confidence_floor)
    except (TypeError, ValueError):
        return False


def _state_is_auto_advanceable(
    state: FSMState, policy: AutonomyPolicy,
) -> bool:
    return state in policy.auto_advance_states


def _kind_not_consent_gated(
    event_kind: str, policy: AutonomyPolicy,
) -> bool:
    return event_kind not in policy.consent_required_kinds


# ---------------------------------------------------------------------------
# Action-specific risk lookup
# ---------------------------------------------------------------------------


def _action_metadata(data: dict[str, Any]) -> dict[str, Any]:
    """Extract action-risk metadata from a TRIG_INFERENCE_DONE
    transition's data payload (which has been populated with the
    action proposal's payload by the inference worker).

    Order of precedence:

    1. Standard actions: look up the ActionTemplate by name and use
       ``intrinsic_amplifiers`` and ``requires_post_review``.
    2. Improvised / Suggestion: read the agent-declared fields from
       the payload (``irreversibility``, ``regret_potential``,
       ``risk_amplifier``).

    Missing values fall back to "high" (most conservative).

    Returns:
        {
          'kind': ActionKind | None,
          'name': str | None,
          'irreversibility': 'low'|'medium'|'high',
          'regret_potential': 'low'|'medium'|'high',
          'risk_amplifier': bool,
          'requires_post_review': bool,
          'estimated_cost_usd': float,
        }
    """
    raw_kind = data.get("kind")
    try:
        kind = ActionKind(raw_kind) if raw_kind else None
    except ValueError:
        kind = None
    name = data.get("name")
    payload_irrev = data.get("irreversibility")
    payload_regret = data.get("regret_potential")
    payload_amplifier = data.get("risk_amplifier")

    # Standard action: try to look up the template
    template = None
    if kind == ActionKind.STANDARD and isinstance(name, str):
        try:
            from work_buddy.threads import actions as _actions
            template = _actions.find_action(name)
        except Exception:
            template = None

    if template is not None:
        amp = template.intrinsic_amplifiers or {}
        irrev = amp.get("irreversibility") or payload_irrev
        regret = amp.get("regret_potential") or payload_regret
        # An action has a "risk amplifier" if any intrinsic amplifier
        # is medium or higher. Standard-action declarations override
        # the agent-supplied flag.
        any_high = any(
            v in ("medium", "high") for v in amp.values()
        )
        risk_amplifier = bool(
            any_high if amp else payload_amplifier
        )
        requires_review = bool(template.requires_post_review)
    else:
        irrev = payload_irrev
        regret = payload_regret
        risk_amplifier = bool(payload_amplifier) if payload_amplifier is not None else False
        requires_review = False

    estimated_cost = 0.0
    raw_cost = data.get("estimated_cost_usd")
    if raw_cost is not None:
        try:
            estimated_cost = float(raw_cost)
        except (TypeError, ValueError):
            estimated_cost = 0.0

    return {
        "kind": kind,
        "name": name,
        "irreversibility": (irrev or "high").lower(),
        "regret_potential": (regret or "high").lower(),
        "risk_amplifier": risk_amplifier,
        "requires_post_review": requires_review,
        "estimated_cost_usd": estimated_cost,
    }


# ---------------------------------------------------------------------------
# Auto-advance decision (returns reason string list for audit)
# ---------------------------------------------------------------------------


def _evaluate_intent_or_context_advance(
    target_state: FSMState,
    policy: AutonomyPolicy,
    data: dict[str, Any],
    event_kind: str,
) -> tuple[bool, list[str]]:
    """Decide whether to skip ``target_state`` (the would-be confirmation)
    and advance.

    Returns ``(advance, axes_passed)``. When ``advance`` is False,
    ``axes_passed`` lists the axes that DID pass (so we can also see
    what blocked when paired with axes_failed)."""
    passed: list[str] = []
    failed: list[str] = []

    if _state_is_auto_advanceable(target_state, policy):
        passed.append("state_in_auto_advance_states")
    else:
        failed.append("state_not_in_auto_advance_states")

    if _confidence_meets_floor(data, policy):
        passed.append("confidence_floor")
    else:
        failed.append("confidence_below_floor")

    if _kind_not_consent_gated(event_kind, policy):
        passed.append("not_consent_gated")
    else:
        failed.append("consent_required_for_kind")

    advance = not failed
    return advance, passed if advance else passed + [f"!{f}" for f in failed]


def _evaluate_action_advance(
    policy: AutonomyPolicy,
    data: dict[str, Any],
    event_kind: str,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Action-specific evaluation.

    Returns ``(advance, axes_traced, action_meta)``. ``advance=True``
    means we can skip ``AWAITING_CONFIRMATION`` and go straight to
    ``EXECUTING``. action_meta is the parsed action metadata (useful
    for the audit event).
    """
    meta = _action_metadata(data)
    base_advance, base_axes = _evaluate_intent_or_context_advance(
        FSMState.AWAITING_CONFIRMATION, policy, data, event_kind,
    )

    extra_passed: list[str] = []
    extra_failed: list[str] = []

    # Action-kind allow-list (a sub-thread may have allowed_action_kinds
    # narrower than the parent's; an improvised action under a
    # Standard-only policy would be blocked).
    if meta["kind"] is not None and meta["kind"] in policy.allowed_action_kinds:
        extra_passed.append("kind_allowed")
    elif meta["kind"] is None:
        extra_failed.append("kind_unknown")
    else:
        extra_failed.append(f"kind_not_allowed:{meta['kind'].value}")

    # Risk thresholds
    if _rank(meta["irreversibility"]) <= _rank(policy.irreversibility_threshold):
        extra_passed.append("irreversibility_within_threshold")
    else:
        extra_failed.append("irreversibility_above_threshold")

    if _rank(meta["regret_potential"]) <= _rank(policy.regret_potential_threshold):
        extra_passed.append("regret_within_threshold")
    else:
        extra_failed.append("regret_above_threshold")

    # Risk amplifier
    if policy.pause_on_risk_amplifier and meta["risk_amplifier"]:
        extra_failed.append("risk_amplifier_present")
    else:
        extra_passed.append("no_risk_amplifier_pause")

    # Post-review actions: don't auto-advance to EXECUTING; user needs
    # to see what's about to happen even if everything else passes.
    if meta["requires_post_review"]:
        # NOTE: we still allow auto-advance through AWAITING_CONFIRMATION
        # — requires_post_review means there's a review AFTER execution,
        # not before. So this is informational, not blocking. Keep the
        # axis name in the trace so the audit log shows it was checked.
        extra_passed.append("post_review_will_apply")

    advance = base_advance and not extra_failed
    axes = base_axes + extra_passed
    if not advance:
        axes += [f"!{f}" for f in extra_failed]
    return advance, axes, meta


# ---------------------------------------------------------------------------
# Audit event
# ---------------------------------------------------------------------------


# Audit shape — populated by the resolver, written by the engine
# AFTER the state_transition event lands. We don't write directly
# from the resolver because the engine has already captured a
# parent_event_id snapshot for its optimistic lock; writing here
# would invalidate that snapshot and cause a spurious conflict.
#
# The engine looks for the special data-key
# ``_autonomy_audit`` on the transition data after running the
# resolver and, if present, appends an ``auto_advance_decision``
# event with that payload using a freshly-read parent_event_id.
_AUDIT_DATA_KEY = "_autonomy_audit"


def _stash_audit(
    data: dict[str, Any],
    *,
    label: str,
    advance: bool,
    chosen_state: FSMState,
    axes: list[str],
    target: str,
    confidence: Optional[float],
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Stash audit metadata on the transition's data dict.

    The engine reads this back after writing state_transition and
    appends an ``auto_advance_decision`` event with a fresh
    parent_event_id. Keeping the resolver pure (no DB writes)
    avoids the optimistic-lock invalidation that happens when an
    unrelated event lands between the engine's snapshot read and
    its state_transition write.
    """
    payload = {
        "branch_label": label,
        "advance": advance,
        "chosen_state": chosen_state.value,
        "axes": axes,
        "target": target,
        "confidence": confidence,
    }
    if extra:
        payload.update(extra)
    data[_AUDIT_DATA_KEY] = payload


# ---------------------------------------------------------------------------
# Public resolvers (called from the engine's branch resolver)
# ---------------------------------------------------------------------------


def resolve_intent_branch(
    thread_id: str, data: dict[str, Any], *, conn=None,
) -> FSMState:
    """Pick AWAITING_INFERENCE (auto-advance to context) vs
    AWAITING_INTENT_CONFIRMATION (surface to user)."""
    from work_buddy.threads.events import KIND_INTENT_INFERRED
    thread = store.get_thread(thread_id, conn=conn)
    if thread is None:
        # No thread → defer to the canonical safe path
        return FSMState.AWAITING_INTENT_CONFIRMATION
    policy = _policy_for(thread, conn=conn)
    advance, axes = _evaluate_intent_or_context_advance(
        FSMState.AWAITING_INTENT_CONFIRMATION, policy, data, KIND_INTENT_INFERRED,
    )
    chosen = FSMState.AWAITING_INFERENCE if advance else FSMState.AWAITING_INTENT_CONFIRMATION
    _stash_audit(
        data,
        label="intent_review_or_advance",
        advance=advance,
        chosen_state=chosen,
        axes=axes,
        target="intent",
        confidence=data.get("confidence"),
    )
    return chosen


def resolve_context_branch(
    thread_id: str, data: dict[str, Any], *, conn=None,
) -> FSMState:
    """Pick AWAITING_INFERENCE (auto-advance to action) vs
    AWAITING_CONTEXT_CONFIRMATION (surface to user)."""
    from work_buddy.threads.events import KIND_CONTEXT_INFERRED
    thread = store.get_thread(thread_id, conn=conn)
    if thread is None:
        return FSMState.AWAITING_CONTEXT_CONFIRMATION
    policy = _policy_for(thread, conn=conn)
    advance, axes = _evaluate_intent_or_context_advance(
        FSMState.AWAITING_CONTEXT_CONFIRMATION, policy, data, KIND_CONTEXT_INFERRED,
    )
    chosen = FSMState.AWAITING_INFERENCE if advance else FSMState.AWAITING_CONTEXT_CONFIRMATION
    _stash_audit(
        data,
        label="context_review_or_advance",
        advance=advance,
        chosen_state=chosen,
        axes=axes,
        target="context",
        confidence=data.get("confidence"),
    )
    return chosen


def resolve_action_branch(
    thread_id: str, data: dict[str, Any], *, conn=None,
) -> FSMState:
    """Pick EXECUTING (auto-execute) vs AWAITING_CONFIRMATION
    (user reviews before execution)."""
    from work_buddy.threads.events import KIND_ACTION_INFERRED
    thread = store.get_thread(thread_id, conn=conn)
    if thread is None:
        return FSMState.AWAITING_CONFIRMATION
    policy = _policy_for(thread, conn=conn)
    advance, axes, meta = _evaluate_action_advance(
        policy, data, KIND_ACTION_INFERRED,
    )
    chosen = FSMState.EXECUTING if advance else FSMState.AWAITING_CONFIRMATION
    _stash_audit(
        data,
        label="action_review_or_execute",
        advance=advance,
        chosen_state=chosen,
        axes=axes,
        target="action",
        confidence=data.get("confidence"),
        extra={
            "action_kind": meta["kind"].value if meta["kind"] else None,
            "action_name": meta["name"],
            "irreversibility": meta["irreversibility"],
            "regret_potential": meta["regret_potential"],
            "risk_amplifier": meta["risk_amplifier"],
            "requires_post_review": meta["requires_post_review"],
        },
    )
    return chosen


# ---------------------------------------------------------------------------
# Branch label registration helper
# ---------------------------------------------------------------------------


_LABEL_TO_RESOLVER = {
    "intent_review_or_advance": resolve_intent_branch,
    "context_review_or_advance": resolve_context_branch,
    "action_review_or_execute": resolve_action_branch,
}


def resolve_by_label(
    label: str, thread_id: str, data: dict[str, Any], *, conn=None,
) -> Optional[FSMState]:
    """Look up the autonomy resolver by branch label.

    Returns None if the label is unknown (caller falls back to the
    engine's default branch resolver). This keeps the autonomy
    resolvers cleanly factored out of the engine.
    """
    fn = _LABEL_TO_RESOLVER.get(label)
    if fn is None:
        return None
    return fn(thread_id, data, conn=conn)
