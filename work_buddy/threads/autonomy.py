"""Autonomy composition — Omegaconf-style merging of policies.

Stage 2.7 deliverable. Per DESIGN.md §11, autonomy is **a per-Thread
policy composed from orthogonal axes**, not an enum. Saved
compositions get names for reuse; the name is just a saved
composition.

Composition rules:
- Global defaults < parent overrides < Thread overrides.
- Merged axis-by-axis (Omegaconf-flavored).
- Sub-threads override DOWN (more conservative); never UP.

Stage 2 ships:
- ``compose(*layers)`` — merge multiple AutonomyPolicy values.
- Three saved compositions: ``end_to_end``, ``plan_then_review``,
  ``hands_off``. Match DESIGN.md §11.2 (loose — these are
  configuration, not types).
- ``override_down(parent, child)`` — validate that the child only
  goes more conservative.
- ``effective_policy_for(thread)`` — resolve the effective policy
  by walking the parent chain.

Composition is currently runtime — no YAML config layer yet. Stage 4
or follow-up may add a config schema for users to author named
compositions in YAML.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Optional

from work_buddy.threads import store
from work_buddy.threads.enums import (
    ActionKind,
    FSMState,
    InvocationContext,
    ReasoningTier,
    tier_at_or_above,
    tier_rank,
)
from work_buddy.threads.events import KIND_ACTION_APPROVED
from work_buddy.threads.models import AutonomyPolicy, Thread


# ---------------------------------------------------------------------------
# Saved compositions (DESIGN.md §11.2)
# ---------------------------------------------------------------------------
#
# These are CONFIGURATION, not types. Adding a new named composition
# is one entry here (or a YAML override later).
# ---------------------------------------------------------------------------


END_TO_END = AutonomyPolicy(
    auto_advance_states=frozenset({
        FSMState.PROPOSED,
        FSMState.AWAITING_INFERENCE,
        FSMState.INFERRING_INTENT,
        FSMState.INFERRING_CONTEXT,
        FSMState.INFERRING_ACTION,
        FSMState.AWAITING_INTENT_CONFIRMATION,
        FSMState.AWAITING_CONTEXT_CONFIRMATION,
        FSMState.AWAITING_CONFIRMATION,
        FSMState.EXECUTING,
    }),
    consent_required_kinds=frozenset(),
    inference_confidence_floor=0.6,
    pause_on_risk_amplifier=True,
    budget_usd=0.50,
)

PLAN_THEN_REVIEW = AutonomyPolicy(
    auto_advance_states=frozenset({
        FSMState.PROPOSED,
        FSMState.AWAITING_INFERENCE,
        FSMState.INFERRING_INTENT,
        FSMState.INFERRING_CONTEXT,
        FSMState.INFERRING_ACTION,
        FSMState.AWAITING_INTENT_CONFIRMATION,
        FSMState.AWAITING_CONTEXT_CONFIRMATION,
    }),
    consent_required_kinds=frozenset({KIND_ACTION_APPROVED}),
    inference_confidence_floor=0.6,  # parity across compositions; the
                                     # override-down rule treats higher
                                     # floor as MORE conservative, and
                                     # plan_then_review is more
                                     # conservative than end_to_end on
                                     # other axes already.
    pause_on_risk_amplifier=True,
    budget_usd=0.50,
)

HANDS_OFF = AutonomyPolicy(
    auto_advance_states=frozenset(),
    consent_required_kinds=frozenset({
        "intent_confirmed", "context_confirmed",
        "action_picked", KIND_ACTION_APPROVED,
    }),
    inference_confidence_floor=0.6,  # same parity. The user gates
                                     # every kind anyway, so the
                                     # floor doesn't materially gate
                                     # behavior — but keeping parity
                                     # lets the override-down lattice
                                     # cleanly order
                                     # end_to_end → plan_then_review →
                                     # hands_off.
    pause_on_risk_amplifier=True,
    budget_usd=0.20,
)


SAVED_COMPOSITIONS: dict[str, AutonomyPolicy] = {
    "end_to_end": END_TO_END,
    "plan_then_review": PLAN_THEN_REVIEW,
    "hands_off": HANDS_OFF,
}


def get_composition(name: str) -> AutonomyPolicy:
    """Resolve a saved composition by name. Raises KeyError if unknown."""
    if name not in SAVED_COMPOSITIONS:
        raise KeyError(
            f"No saved composition {name!r}. Known: "
            f"{sorted(SAVED_COMPOSITIONS)}",
        )
    return SAVED_COMPOSITIONS[name]


# ---------------------------------------------------------------------------
# Composition merging
# ---------------------------------------------------------------------------


# A sentinel marker used by ``compose`` to distinguish "this layer
# wants the default kept" from "this layer explicitly sets a value."
# AutonomyPolicy is a frozen dataclass; layers are AutonomyPolicy
# instances or partial dicts. compose accepts both.
_AXIS_NAMES: tuple[str, ...] = (
    "auto_advance_states",
    "consent_required_kinds",
    "inference_confidence_floor",
    "irreversibility_threshold",
    "regret_potential_threshold",
    "pause_on_risk_amplifier",
    "allowed_action_kinds",
    "allowed_invocation_contexts",
    "inference_floor_tier",
    "inference_ceiling_tier",
    "budget_usd",
)


def compose(*layers: AutonomyPolicy | dict | None) -> AutonomyPolicy:
    """Merge layered policies axis-by-axis. Later layers override.

    Each layer may be:
    - ``AutonomyPolicy`` (every axis sets) — replaces the whole
      base for the layer's axes.
    - ``dict`` — partial; only the keys present in the dict
      override the merged base.
    - ``None`` — no-op (skipped).

    Returns a fresh AutonomyPolicy. Original layers are not
    mutated.

    The base layer (when no layers are provided, or layer 0 is
    None) is the default ``AutonomyPolicy()``.
    """
    base: AutonomyPolicy = AutonomyPolicy()
    for layer in layers:
        if layer is None:
            continue
        if isinstance(layer, AutonomyPolicy):
            # Full override — replace every axis the new layer carries.
            kwargs = {
                axis: getattr(layer, axis) for axis in _AXIS_NAMES
            }
            base = replace(base, **kwargs)
        elif isinstance(layer, dict):
            kwargs = {}
            for axis in _AXIS_NAMES:
                if axis in layer:
                    kwargs[axis] = layer[axis]
            base = replace(base, **kwargs) if kwargs else base
        else:
            raise TypeError(
                f"compose() got unsupported layer type {type(layer)!r}",
            )
    return base


# ---------------------------------------------------------------------------
# Override-down validation (DESIGN.md §11.3)
# ---------------------------------------------------------------------------


_RISK_RANK = {"low": 0, "medium": 1, "high": 2}


class OverrideUpRejected(ValueError):
    """A child sub-thread tried to widen autonomy. Refused."""


def _is_more_conservative_or_equal(
    axis: str, parent_value, child_value,
) -> bool:
    """Per-axis comparator — True iff child is at least as
    conservative as parent on this axis.

    Direction definitions (more-conservative is the LATTER):
    - auto_advance_states: subset (fewer auto-advance = more conservative).
    - consent_required_kinds: superset (more consent gates = more conservative).
    - allowed_action_kinds: subset.
    - allowed_invocation_contexts: subset.
    - inference_confidence_floor: lower (be more cautious about acting on weak inferences) — more conservative is HIGHER (refuse low-confidence).

      Note: this is the trickiest axis. A higher floor means
      "I won't act unless inference is very confident", which is
      MORE conservative. So child >= parent.
    - irreversibility_threshold: lower index = more conservative
      (low < medium < high; child's threshold should be ≤ parent's).
    - regret_potential_threshold: same direction as irreversibility.
    - pause_on_risk_amplifier: True is more conservative; child must
      have True if parent does.
    - inference_floor_tier: a HIGHER floor means the cheapest
      tier the engine is allowed to run; child's floor ≥ parent's
      is more conservative (don't go cheaper).
    - inference_ceiling_tier: LOWER ceiling = more conservative
      (cap escalation lower); child's ceiling ≤ parent's.
    - budget_usd: lower budget = more conservative; child ≤ parent.
    """
    if axis == "auto_advance_states":
        return set(child_value).issubset(set(parent_value))
    if axis == "consent_required_kinds":
        return set(child_value).issuperset(set(parent_value))
    if axis == "allowed_action_kinds":
        return set(child_value).issubset(set(parent_value))
    if axis == "allowed_invocation_contexts":
        return set(child_value).issubset(set(parent_value))
    if axis == "inference_confidence_floor":
        return float(child_value) >= float(parent_value)
    if axis in ("irreversibility_threshold", "regret_potential_threshold"):
        return _RISK_RANK[child_value] <= _RISK_RANK[parent_value]
    if axis == "pause_on_risk_amplifier":
        # parent True → child must also be True
        if parent_value and not child_value:
            return False
        return True
    if axis == "inference_floor_tier":
        return tier_at_or_above(child_value, parent_value)
    if axis == "inference_ceiling_tier":
        return tier_rank(child_value) <= tier_rank(parent_value)
    if axis == "budget_usd":
        return float(child_value) <= float(parent_value)
    raise KeyError(f"Unknown axis {axis!r}")


def is_override_down(parent: AutonomyPolicy, child: AutonomyPolicy) -> bool:
    """True if every axis in ``child`` is at least as conservative
    as the same axis in ``parent``."""
    for axis in _AXIS_NAMES:
        if not _is_more_conservative_or_equal(
            axis, getattr(parent, axis), getattr(child, axis),
        ):
            return False
    return True


def violations_for_override_down(
    parent: AutonomyPolicy, child: AutonomyPolicy,
) -> list[str]:
    """Per-axis list of violations for diagnostics. Empty list iff valid."""
    violations: list[str] = []
    for axis in _AXIS_NAMES:
        if not _is_more_conservative_or_equal(
            axis, getattr(parent, axis), getattr(child, axis),
        ):
            violations.append(axis)
    return violations


def validate_override_down(
    parent: AutonomyPolicy, child: AutonomyPolicy,
) -> None:
    """Raise :class:`OverrideUpRejected` if the child widens autonomy.

    Used by the decompose action when spawning sub-threads.
    """
    bad = violations_for_override_down(parent, child)
    if bad:
        raise OverrideUpRejected(
            "Sub-thread tried to widen autonomy on axes: "
            f"{', '.join(bad)}",
        )


# ---------------------------------------------------------------------------
# Effective-policy resolution (walk parent chain)
# ---------------------------------------------------------------------------


def effective_policy_for(
    thread: Thread,
    *,
    walk_parents: bool = True,
    max_depth: int = 16,
    conn=None,
) -> AutonomyPolicy:
    """Return the effective policy for ``thread``.

    With ``walk_parents=True`` (default), this composes:

        global_default < parent's parent ... < direct parent < thread

    Each layer overrides the previous axis-by-axis. The result is
    the most-conservative legal policy under the override-down rule.

    Without ``walk_parents`` (or when the Thread has no parent),
    returns the Thread's policy unchanged.
    """
    if not walk_parents or thread.parent_id is None:
        return thread.autonomy_policy

    chain: list[AutonomyPolicy] = []
    current: Optional[Thread] = thread
    depth = 0
    while current is not None and depth < max_depth:
        chain.append(current.autonomy_policy)
        if current.parent_id is None:
            break
        current = store.get_thread(current.parent_id, conn=conn)
        depth += 1
    # Reverse so root → leaf order
    return compose(*reversed(chain))
