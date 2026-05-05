"""Dataclasses for the Thread system.

Stage 1 deliverable: frozen type signatures so downstream stages can
program against them. No behavior wired here — those land in Stage 2
(FSM engine, inference layer, sidecar workers).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from work_buddy.threads.enums import (
    ActionKind,
    Authorship,
    FSMState,
    InvocationContext,
    ReasoningTier,
    SurfaceUrgency,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_thread_id() -> str:
    """Stable opaque ID. The ``th-`` prefix lets a glance distinguish
    Threads from Tasks (``t-``) and ContextItems (``ctx-``)."""
    return f"th-{uuid.uuid4().hex[:8]}"


def _new_event_id() -> int:
    """Event IDs come from the DB ``AUTOINCREMENT`` column. This helper
    exists for tests that need a deterministic shape; the canonical
    source is the database."""
    raise NotImplementedError(
        "Event IDs are assigned by the DB; do not synthesize.",
    )


# ---------------------------------------------------------------------------
# ContextItem (DESIGN.md §12)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextItem:
    """Typed per-item primitive across all context sources.

    Stable across collection runs. The drag-and-drop migration
    operation (DESIGN.md §12.4) operates on these.
    """

    id: str               # source-specific, stable across runs
    source: str           # 'chrome' | 'tasks' | 'git' | 'projects' | 'smart' | ...
    type: str             # 'tab' | 'task' | 'commit' | 'project' | 'contract' | ...
    label: str            # human-readable title for UI
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "type": self.type,
            "label": self.label,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContextItem:
        return cls(
            id=d["id"],
            source=d["source"],
            type=d["type"],
            label=d["label"],
            payload=d.get("payload") or {},
        )


# ---------------------------------------------------------------------------
# AutonomyPolicy (DESIGN.md §11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutonomyPolicy:
    """Per-Thread policy composed from orthogonal axes.

    Saved compositions (``end_to_end``, ``plan_then_review``,
    ``hands_off``) are configuration, NOT types — see
    ``work_buddy.threads.autonomy`` (Stage 2) for composition logic.

    Composition is Omegaconf-flavored: global defaults < parent
    overrides < Thread overrides; merged axis-by-axis.
    """

    # Per FSM state: should the engine auto-advance or wait for user?
    auto_advance_states: frozenset[FSMState] = frozenset()

    # Per event kind: always require user input before this lands?
    consent_required_kinds: frozenset[str] = frozenset()

    # Confidence floor: below this, escalate to user (transition to clarify)
    inference_confidence_floor: float = 0.3

    # Risk thresholds: above these, force user confirmation
    irreversibility_threshold: str = "medium"  # 'low' | 'medium' | 'high'
    regret_potential_threshold: str = "medium"
    pause_on_risk_amplifier: bool = True

    # Action kinds allowed
    allowed_action_kinds: frozenset[ActionKind] = frozenset({
        ActionKind.STANDARD,
        ActionKind.IMPROVISED,
        ActionKind.SUGGESTION,
    })

    # Contexts in which this Thread can dispatch actions
    allowed_invocation_contexts: frozenset[InvocationContext] = frozenset({
        InvocationContext.ACTION_PROPOSAL,
        InvocationContext.AGENT_AUTONOMOUS,
    })

    # Reasoning-tier guardrails (per-Thread; per-call escalation
    # within these bounds is the existing LLM-call escalation policy).
    inference_floor_tier: ReasoningTier = ReasoningTier.FRONTIER_FAST
    inference_ceiling_tier: ReasoningTier = ReasoningTier.AGENT_HEADLESS

    # Budget axis: enforced at LLM-call enqueue (DESIGN.md §9.4)
    budget_usd: float = 0.50

    # combined-inference opt-in. When True, the inference
    # worker dispatches a single LLM call with InferenceTarget.COMBINED
    # that returns intent + context + action together, then walks the
    # FSM through inferring_* states without re-enqueuing. Default
    # False (stage inference target by target). Source pipelines
    # may opt in based on the inciting context (e.g. multi-tab
    # Chrome scrapes benefit from seeing all tabs at once).
    combined_inference: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "auto_advance_states": sorted(s.value for s in self.auto_advance_states),
            "consent_required_kinds": sorted(self.consent_required_kinds),
            "inference_confidence_floor": self.inference_confidence_floor,
            "irreversibility_threshold": self.irreversibility_threshold,
            "regret_potential_threshold": self.regret_potential_threshold,
            "pause_on_risk_amplifier": self.pause_on_risk_amplifier,
            "allowed_action_kinds": sorted(k.value for k in self.allowed_action_kinds),
            "allowed_invocation_contexts": sorted(
                c.value for c in self.allowed_invocation_contexts
            ),
            "inference_floor_tier": self.inference_floor_tier.value,
            "inference_ceiling_tier": self.inference_ceiling_tier.value,
            "budget_usd": self.budget_usd,
            "combined_inference": self.combined_inference,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AutonomyPolicy:
        return cls(
            auto_advance_states=frozenset(
                FSMState(s) for s in d.get("auto_advance_states", [])
            ),
            consent_required_kinds=frozenset(d.get("consent_required_kinds", [])),
            inference_confidence_floor=float(
                d.get("inference_confidence_floor", 0.3)
            ),
            irreversibility_threshold=d.get("irreversibility_threshold", "medium"),
            regret_potential_threshold=d.get("regret_potential_threshold", "medium"),
            pause_on_risk_amplifier=bool(d.get("pause_on_risk_amplifier", True)),
            allowed_action_kinds=frozenset(
                ActionKind(k) for k in d.get("allowed_action_kinds", [])
            ) or frozenset(ActionKind),
            allowed_invocation_contexts=frozenset(
                InvocationContext(c) for c in d.get("allowed_invocation_contexts", [])
            ) or frozenset(InvocationContext),
            inference_floor_tier=ReasoningTier(
                d.get("inference_floor_tier", "frontier_fast")
            ),
            inference_ceiling_tier=ReasoningTier(
                d.get("inference_ceiling_tier", "agent_headless")
            ),
            budget_usd=float(d.get("budget_usd", 0.50)),
            combined_inference=bool(d.get("combined_inference", False)),
        )


# ---------------------------------------------------------------------------
# Thread (DESIGN.md §5)
# ---------------------------------------------------------------------------


@dataclass
class Thread:
    """The universal entity for "context that may need an action."

    Subtype is set at creation, never mutated; the only named
    subtype is ``Task``.
    """

    thread_id: str = field(default_factory=_new_thread_id)
    parent_id: Optional[str] = None
    subtype: Optional[str] = None  # 'task' | None; never mutated
    fsm_state: FSMState = FSMState.PROPOSED

    # Last-known FSM-event id — used as the optimistic-lock target on
    # the next state transition. None for never-transitioned threads.
    parent_event_id: Optional[int] = None

    autonomy_policy: AutonomyPolicy = field(default_factory=AutonomyPolicy)

    # Attached ContextItems (live in their source; this is just a
    # reference list).
    context_items: tuple[ContextItem, ...] = ()

    # Risk profile — per DESIGN.md §10.4 the thread carries
    # contextual risk dimensions; intrinsic amplifiers live on the
    # action template and are composed at execution time.
    risk_profile: dict[str, Any] = field(default_factory=dict)

    # Inciting-event metadata: what brought this Thread into being.
    # Just a dict; canonical full-fidelity record lives in the event
    # log's ``inciting_event`` row.
    inciting_event_summary: dict[str, Any] = field(default_factory=dict)

    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    archived_at: Optional[str] = None

    # If this Thread had its current_action_item-equivalent set by a
    # parent (legacy ``current_action_item_id`` semantics, surfaced in
    # the bridge). Stored as a Thread ID pointing at a sub-Thread.
    current_focus_thread_id: Optional[str] = None

    # Stage 4 fields (UX.md §8.2 + §10.2 + §13).
    resurface_at: Optional[str] = None        # Later mechanic
    order_index: int = 0                       # write-time linearization
    search_blob: str = ""                      # denormalized search

    # parent-child relationship discriminator. 'decompose' is
    # the canonical fanout pattern (parent → action → N children, each
    # FSM-executes; cascade-on-terminal advances parent to DONE). 'group'
    # is the new pattern: parent is a re-organisable container; items
    # can move between sibling group-parents via move_thread_to_parent.
    # Default 'decompose' preserves all v4/Stage-4 behaviour.
    parent_relationship: str = "decompose"

    # sibling-scope id. Group-parents from one inference run
    # share an originating_scrape_id (e.g. one Chrome scrape → N
    # group-parents, all with the same id). Items can only move
    # between parents that share this id. NULL for decompose parents
    # and pre-Stage-5 data.
    originating_scrape_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Convenience predicates
    # ------------------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.fsm_state.is_terminal

    @property
    def is_task(self) -> bool:
        return self.subtype == "task"

    @property
    def is_group_parent(self) -> bool:
        """True iff this Thread is a group-relationship parent.

        Stage 5 helper for the move-between-groups op + the cascade
        auto-DISMISS branch. Note: leaf threads (those with parent_id
        set) carry their own ``parent_relationship``, but it's only
        meaningful when this Thread itself acts as a parent. Callers
        should typically check ``parent_id IS NULL`` first or simply
        consult the parent before allowing a move.
        """
        return self.parent_relationship == "group"

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "parent_id": self.parent_id,
            "subtype": self.subtype,
            "fsm_state": self.fsm_state.value,
            "parent_event_id": self.parent_event_id,
            "autonomy_policy": self.autonomy_policy.to_dict(),
            "context_items": [c.to_dict() for c in self.context_items],
            "risk_profile": self.risk_profile,
            "inciting_event_summary": self.inciting_event_summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
            "current_focus_thread_id": self.current_focus_thread_id,
            "resurface_at": self.resurface_at,
            "order_index": self.order_index,
            "search_blob": self.search_blob,
            "parent_relationship": self.parent_relationship,
            "originating_scrape_id": self.originating_scrape_id,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Thread:
        """Rehydrate from a sqlite3 Row dict (Stage 1.3 schema).

        ``autonomy_policy_json``, ``context_items_json``, and
        ``risk_profile_json`` are stored serialised; this helper
        deserialises them.
        """

        def _load_json(value: Any, default: Any) -> Any:
            if value is None:
                return default
            if isinstance(value, (dict, list)):
                return value
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return default

        return cls(
            thread_id=row["thread_id"],
            parent_id=row.get("parent_id"),
            subtype=row.get("subtype"),
            fsm_state=FSMState(row.get("fsm_state") or FSMState.PROPOSED.value),
            parent_event_id=row.get("parent_event_id"),
            autonomy_policy=AutonomyPolicy.from_dict(
                _load_json(row.get("autonomy_policy_json"), {}),
            ),
            context_items=tuple(
                ContextItem.from_dict(c)
                for c in _load_json(row.get("context_items_json"), [])
            ),
            risk_profile=_load_json(row.get("risk_profile_json"), {}),
            inciting_event_summary=_load_json(
                row.get("inciting_event_summary_json"), {},
            ),
            created_at=row.get("created_at") or _now_iso(),
            updated_at=row.get("updated_at") or _now_iso(),
            archived_at=row.get("archived_at"),
            current_focus_thread_id=row.get("current_focus_thread_id"),
            resurface_at=row.get("resurface_at"),
            order_index=row.get("order_index") or 0,
            search_blob=row.get("search_blob") or "",
            parent_relationship=row.get("parent_relationship") or "decompose",
            originating_scrape_id=row.get("originating_scrape_id"),
        )


@dataclass
class Task(Thread):
    """A Thread with the master-task-list contract.

    See DESIGN.md §5.3. Adds:
    - markdown sync to the master task list
    - persistence across terminal state (does not auto-archive)
    - surface in the Tasks dashboard tab

    Subtype is fixed at ``'task'``; do not mutate.

    type only. Stage 2 wires the methods.
    """

    subtype: str = "task"

    def sync_to_markdown(self) -> None:
        """Write this Task's representation to the master task list.

        Stage 2 work; bridge integration lands then.
        """
        raise NotImplementedError(
            "Task.sync_to_markdown is wired in Stage 2.",
        )

    def master_list_position(self) -> int:
        """Return the 1-based position of this Task in the master list.

        Stage 2 work.
        """
        raise NotImplementedError(
            "Task.master_list_position is wired in Stage 2.",
        )


# ---------------------------------------------------------------------------
# ResolutionRequest (DESIGN.md §7.3, §15.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolutionRequest:
    """Typed message published when a Thread enters a wait state.

    Flows through the existing consent subsystem (DESIGN.md §7.3).
    Carries enough payload for the Resolution Surface card renderer
    to build the right card type (DESIGN.md §15.1).
    """

    thread_id: str
    fsm_state: FSMState
    proposing_actor: Optional[str]  # 'agent' | 'user' | None
    urgency: SurfaceUrgency
    payload: dict[str, Any] = field(default_factory=dict)
    deadline: Optional[str] = None  # ISO 8601, for time-sensitive
    parent_event_id: Optional[int] = None  # optimistic-lock target

    def card_kind(self) -> str:
        """Resolution Surface card type derived from FSM state.

        ``confirmation`` | ``clarification`` | ``consent`` |
        ``review`` | ``redirect``.
        """
        s = self.fsm_state
        if s == FSMState.AWAITING_CONFIRMATION:
            return "consent"
        if s == FSMState.AWAITING_REVIEW:
            return "review"
        if s == FSMState.AWAITING_REDIRECT:
            return "redirect"
        if s.is_confirmation_state:
            return "confirmation"
        if s.is_clarification_state:
            return "clarification"
        # Unexpected: state isn't a wait state at all.
        raise ValueError(f"State {s.value} is not a wait state")

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "fsm_state": self.fsm_state.value,
            "proposing_actor": self.proposing_actor,
            "urgency": self.urgency.value,
            "payload": self.payload,
            "deadline": self.deadline,
            "parent_event_id": self.parent_event_id,
            "card_kind": self.card_kind(),
        }


# ---------------------------------------------------------------------------
# Proposal (DESIGN.md §9.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Proposal:
    """Inference output. Recorded as a ``*_inferred`` event with full
    provenance.

    type. inference layer produces these.
    """

    target: str  # InferenceTarget value
    payload: dict[str, Any]
    confidence: float
    tier_used: ReasoningTier
    model_used: Optional[str] = None  # e.g. 'claude-sonnet-4-6'
    cost_usd: float = 0.0
    reasoning_trace_pointer: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "payload": self.payload,
            "confidence": self.confidence,
            "tier_used": self.tier_used.value,
            "model_used": self.model_used,
            "cost_usd": self.cost_usd,
            "reasoning_trace_pointer": self.reasoning_trace_pointer,
        }
