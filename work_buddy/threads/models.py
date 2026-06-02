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
from work_buddy.threads.workitem import WorkItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_thread_id() -> str:
    """Stable opaque ID. The ``th-`` prefix lets a glance distinguish
    Threads from Tasks (``t-``) and ContextItems (``ctx-``)."""
    return f"th-{uuid.uuid4().hex[:8]}"


def _new_task_id() -> str:
    """Stable opaque Task ID — same ``t-<hex8>`` shape as the live task
    system's ``obsidian.tasks.mutations.generate_task_id`` (mirrored here
    to avoid importing the heavy obsidian.tasks package at models load)."""
    return f"t-{uuid.uuid4().hex[:8]}"


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
class Thread(WorkItem):
    """The FSM-resolution subtype of :class:`WorkItem`.

    Inherits the universal fields from ``WorkItem`` (id, lineage,
    autonomy policy, context, risk profile, lifecycle timestamps,
    resurface/order/search) and adds the resolution-FSM machinery
    below. Subtype is set at creation, never mutated; the only named
    subtype is ``Task`` (a sibling on ``WorkItem``, not a child of
    ``Thread``).
    """

    # Re-declared only to pin the ``th-`` id prefix — WorkItem's own
    # default is the generic ``wi-``. The field keeps its (first)
    # position from the base; only the default factory changes.
    thread_id: str = field(default_factory=_new_thread_id)

    # --- Thread-specific (resolution-FSM) fields -----------------------
    # The universal fields (parent_id, subtype, autonomy_policy,
    # context_items, risk_profile, inciting_event_summary, created_at,
    # updated_at, archived_at, resurface_at, order_index, search_blob)
    # are inherited from WorkItem unchanged.

    fsm_state: FSMState = FSMState.PROPOSED

    # Last-known FSM-event id — used as the optimistic-lock target on
    # the next state transition. None for never-transitioned threads.
    parent_event_id: Optional[int] = None

    # If this Thread had its current_action_item-equivalent set by a
    # parent (legacy ``current_action_item_id`` semantics, surfaced in
    # the bridge). Stored as a Thread ID pointing at a sub-Thread.
    current_focus_thread_id: Optional[str] = None

    # parent-child relationship discriminator. 'decompose' is
    # the canonical fanout pattern (parent → action → N children, each
    # FSM-executes; cascade-on-terminal advances parent to DONE). 'group'
    # is the new pattern: parent is a re-organisable container; items
    # can move between sibling group-parents via move_thread_to_parent.
    # Default 'decompose' preserves the original fanout behaviour.
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

    # ``is_task`` is inherited from WorkItem (reads ``subtype`` only).

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
        # Universal fields come from the base projection; Thread adds its
        # resolution-FSM keys. The combined output is the same 18-key dict
        # as before the WorkItem extraction (key order is irrelevant to
        # equality; the golden master asserts this).
        d = self._universal_dict()
        d.update({
            "fsm_state": self.fsm_state.value,
            "parent_event_id": self.parent_event_id,
            "current_focus_thread_id": self.current_focus_thread_id,
            "parent_relationship": self.parent_relationship,
            "originating_scrape_id": self.originating_scrape_id,
        })
        return d

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
class Task(WorkItem):
    """The master-list-contract subtype of :class:`WorkItem`.

    A Task is a **sibling** of :class:`Thread` on ``WorkItem`` — NOT a
    ``Thread`` subclass. This is the WorkItem inversion: the heavy
    resolution FSM stays on ``Thread``; ``Task`` has **no FSM**.
    Its lifecycle is the task system's own state vocab (inbox / mit /
    focused / snoozed / done) and it persists in the ``obsidian/tasks``
    task_metadata store + the markdown master list — **never** in the
    ``threads`` table.

    This type is a *transitional facade*: it wraps existing task rows and
    reads through the live task store; the markdown sync adapter +
    write-delegation are extracted when the facade is later collapsed onto
    an owned adapter. Field-ownership
    follows ``TaskMarkdownDB.FIELDS`` — the Obsidian Tasks plugin owns its
    in-markdown markers (checkbox / dates / recurrence / priority); this
    facade never fights the plugin.

    Subtype is fixed ``'task'`` and never mutated.
    """

    subtype: str = "task"
    # ``t-`` prefix (overrides WorkItem's generic ``wi-``); matches the
    # live task store's id shape so a Task wraps its existing row 1:1.
    thread_id: str = field(default_factory=_new_task_id)

    @classmethod
    def from_store_row(cls, row: dict[str, Any]) -> "Task":
        """Build a Task facade from a live ``task_metadata`` row dict (the
        shape returned by ``obsidian.tasks.store.get`` / ``.query``).

        Maps the store's columns onto the WorkItem universal slots. Task
        content with no WorkItem slot (state, urgency, contract, the
        plugin-owned markers, …) stays in the store and is read live via
        :meth:`live_row` — the facade does not duplicate it.
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
            thread_id=row["task_id"],
            parent_id=row.get("parent_id"),
            risk_profile=_load_json(row.get("risk_profile_json"), {}),
            created_at=row.get("created_at") or _now_iso(),
            updated_at=row.get("updated_at") or _now_iso(),
            archived_at=row.get("archived_at"),
        )

    def live_row(self) -> Optional[dict[str, Any]]:
        """Read this Task's authoritative row from the live task store.

        The markdown-backed store is the source of truth for task content
        + state; the facade never caches it. Lazily imports the task store
        so ``threads.models`` stays decoupled from ``obsidian.tasks`` at
        import time. Returns ``None`` if the task is absent / deleted.
        """
        from work_buddy.obsidian.tasks import store as _task_store

        return _task_store.get(self.thread_id)

    # ------------------------------------------------------------------
    # Write surface — a Task is mutated *as a WorkItem*, through the
    # task write port (``work_item.task_adapter``). The port delegates to
    # the live mutation layer, which owns the atomic dual-surface write,
    # plugin-marker preservation, consent, bridge-retry, and event
    # emission — so these methods add no behaviour of their own. The
    # adapter is imported inside each method to keep ``threads.models``
    # decoupled from it at import time (and cycle-free).
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, task_id: str) -> Optional["Task"]:
        """Build a Task facade from a ``task_id`` by reading the live store.

        Returns ``None`` if the task is absent or soft-deleted. Reuses
        :meth:`from_store_row`; lazily imports the store (same decoupling as
        :meth:`live_row`).
        """
        from work_buddy.obsidian.tasks import store as _task_store

        row = _task_store.get(task_id)
        return cls.from_store_row(row) if row is not None else None

    @classmethod
    def create(
        cls,
        task_text: str,
        *,
        urgency: str = "medium",
        project: Optional[str] = None,
        due_date: Optional[str] = None,
        contract: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a new task through the WorkItem write port.

        A classmethod — there is no Task yet (no ``thread_id`` to act on); the
        id is minted inside the mutation layer's idempotency cache, so this
        never generates its own. Returns the raw ``create_task`` result dict
        (the minted ``task_id`` + verification state callers consume), NOT a
        Task; a caller wanting the object does ``Task.load(result["task_id"])``.
        The GTD/risk keyword tail is forwarded via ``**kwargs``.
        """
        from work_buddy.work_item import task_adapter

        return task_adapter.create(
            task_text,
            urgency=urgency,
            project=project,
            due_date=due_date,
            contract=contract,
            summary=summary,
            tags=tags,
            **kwargs,
        )

    def toggle(
        self,
        done: Optional[bool] = None,
        *,
        file_path: Optional[str] = None,
        done_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """Toggle this task's completion through the WorkItem write port."""
        from work_buddy.work_item import task_adapter

        return task_adapter.toggle(
            self.thread_id, done=done, file_path=file_path, done_date=done_date,
        )

    def update(
        self,
        *,
        state: Optional[str] = None,
        urgency: Optional[str] = None,
        complexity: Optional[str] = None,
        contract: Optional[str] = None,
        snooze_until: Optional[str] = None,
        due_date: Optional[str] = None,
        reason: Optional[str] = None,
        file_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Update this task's metadata through the WorkItem write port.

        Cannot set ``state='done'`` — the mutation layer rejects it; use
        :meth:`toggle` for completion.
        """
        from work_buddy.work_item import task_adapter

        return task_adapter.update(
            self.thread_id,
            state=state,
            urgency=urgency,
            complexity=complexity,
            contract=contract,
            snooze_until=snooze_until,
            due_date=due_date,
            reason=reason,
            file_path=file_path,
        )

    def set_description(
        self, text: str, *, file_path: Optional[str] = None,
    ) -> dict[str, Any]:
        """Rewrite this task's description text through the WorkItem write port."""
        from work_buddy.work_item import task_adapter

        return task_adapter.set_description(
            self.thread_id, text, file_path=file_path,
        )

    def set_tags(self, namespace_tags: list[str]) -> dict[str, Any]:
        """Replace this task's user-modifiable tags through the WorkItem write port."""
        from work_buddy.work_item import task_adapter

        return task_adapter.set_tags(self.thread_id, namespace_tags)

    def delete(self) -> dict[str, Any]:
        """Delete this task (line, note, store record) through the WorkItem write port."""
        from work_buddy.work_item import task_adapter

        return task_adapter.delete(self.thread_id)

    def assign(self) -> dict[str, Any]:
        """Claim this task for the current agent session through the WorkItem write port."""
        from work_buddy.work_item import task_adapter

        return task_adapter.assign(self.thread_id)


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
