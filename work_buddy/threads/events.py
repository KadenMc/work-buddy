"""Thread event log — types, kinds, and helpers.

Append-only event log; every state-affecting operation lands an event.
The log is the canonical source; the Thread's current state cache
exists for query convenience but events are authoritative.

See DESIGN.md §13. The schema for ``thread_events`` lands in Stage 1.3
(``work_buddy/threads/store.py``); this module defines the
in-memory/wire types and the event-kind catalog.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Event kinds — the canonical catalog
# ---------------------------------------------------------------------------
#
# Grouped to mirror DESIGN.md §13.2. Use the named constants instead of
# raw strings everywhere.
# ---------------------------------------------------------------------------

# Lifecycle
KIND_INCITING_EVENT = "inciting_event"
KIND_THREAD_CREATED = "thread_created"
KIND_THREAD_DISMISSED = "thread_dismissed"
KIND_THREAD_COMPLETED = "thread_completed"
KIND_THREAD_HANDED_OFF = "thread_handed_off"
KIND_THREAD_ARCHIVED = "thread_archived"

# Inference
KIND_INTENT_INFERRED = "intent_inferred"
KIND_CONTEXT_INFERRED = "context_inferred"
KIND_ACTION_INFERRED = "action_inferred"

# Confirmation
KIND_INTENT_CONFIRMED = "intent_confirmed"
KIND_INTENT_CORRECTED = "intent_corrected"
KIND_CONTEXT_CONFIRMED = "context_confirmed"
KIND_CONTEXT_EDITED = "context_edited"
KIND_ACTION_CONFIRMED = "action_confirmed"
KIND_ACTION_CORRECTED = "action_corrected"

# Clarification
KIND_INTENT_PROVIDED = "intent_provided"
KIND_CONTEXT_PROVIDED = "context_provided"
KIND_ACTION_PICKED = "action_picked"

# Redirect
KIND_INTENT_REDIRECTED = "intent_redirected_with_feedback"
KIND_CONTEXT_REDIRECTED = "context_redirected_with_feedback"
KIND_ACTION_REDIRECTED = "action_redirected_with_feedback"
KIND_REVIEW_REDIRECTED = "review_redirected_with_feedback"

# Consent (action gate)
KIND_ACTION_APPROVED = "action_approved"
KIND_ACTION_REJECTED = "action_rejected_with_feedback"

# Execution (sparingly — see DESIGN.md §8.3)
KIND_EXECUTION_STARTED = "execution_started"
KIND_EXECUTION_FINISHED = "execution_finished"

# Migration (cross-Thread atomic ops; share a migration_id)
KIND_CONTEXT_ADDED = "context_added"
KIND_CONTEXT_REMOVED = "context_removed"

# Decomposition / hierarchy
KIND_SUBTHREADS_SPAWNED = "subthreads_spawned"
KIND_SUBTHREAD_TERMINAL_REPORTED = "subthread_terminal_reported"
KIND_PARENT_FORCE_CLOSE = "parent_force_close"

# Legacy thread-level move between sibling group-parents. Kept for
# backward compat with any old scrapes still in DBs that haven't been
# wiped; no new code emits this kind.
KIND_ITEM_MOVED = "item_moved"

# Group-relationship spawn: emitted on ``threads.group.group_thread``
# when an umbrella spawns its children. Mirrors
# KIND_SUBTHREADS_SPAWNED's payload shape:
#   {child_thread_ids: [...], child_labels: [...], source_count: int,
#    cluster_count: int, [user_created: bool]}
KIND_GROUPS_SPAWNED = "groups_spawned"

# A single ContextItem moved between two sibling group children.
# Paired events on src + dest share a migration_id. Data carries
# direction ("out"|"in"), item_id, src_thread_id, dest_thread_id,
# and umbrella_id.
KIND_CONTEXT_ITEM_MOVED = "context_item_moved"

# User explicitly deleted a group child via the header X button.
# Recorded on the umbrella for audit. Data carries deleted_child_id
# and had_items count.
KIND_GROUP_DELETED = "group_deleted"

# Universal-action audit. Recorded by ``threads.universal_actions.thread_rename``
# whenever the title is changed (e.g. via the action-chip "Rename" affordance,
# or by the LLM cluster-refinement step overriding an algorithmic cluster
# label).
KIND_THREAD_RENAMED = "thread_renamed"

# Budget / loop
KIND_BUDGET_WARNING = "budget_warning"
KIND_LOOP_DETECTED = "loop_detected"
KIND_ESCALATED_TO_USER = "escalated_to_user"

# State transition (catch-all when the change is purely a state move,
# e.g. queue dispatch -> inferring_*).
KIND_STATE_TRANSITION = "state_transition"

# Later mechanic + cleanup events.
KIND_LATER = "later"
KIND_SOURCE_CLEANED_UP = "source_cleaned_up"
KIND_CLEANUP_FAILED = "cleanup_failed"

# Stage 5 (autonomy runtime): per-decision audit emitted by the
# autonomy-gated branch resolvers. Records which axes passed/failed
# when the FSM picked between auto-advance and surfacing a card.
# Never user-facing in the dashboard list (filtered like
# state_transition); visible in the thread detail view + the
# mid-process toggle.
KIND_AUTO_ADVANCE_DECISION = "auto_advance_decision"

# Stage 5 (combined inference): emitted alongside the three
# *_inferred events when a single LLM call satisfied all three
# targets at once. Carries the per-target attribution of the
# combined call so the audit trace remains honest about
# "this was one call, not three."
KIND_COMBINED_INFERRED_META = "combined_inferred_meta"


ALL_KINDS: frozenset[str] = frozenset({
    KIND_INCITING_EVENT,
    KIND_THREAD_CREATED,
    KIND_THREAD_DISMISSED,
    KIND_THREAD_COMPLETED,
    KIND_THREAD_HANDED_OFF,
    KIND_THREAD_ARCHIVED,
    KIND_INTENT_INFERRED,
    KIND_CONTEXT_INFERRED,
    KIND_ACTION_INFERRED,
    KIND_INTENT_CONFIRMED,
    KIND_INTENT_CORRECTED,
    KIND_CONTEXT_CONFIRMED,
    KIND_CONTEXT_EDITED,
    KIND_ACTION_CONFIRMED,
    KIND_ACTION_CORRECTED,
    KIND_INTENT_PROVIDED,
    KIND_CONTEXT_PROVIDED,
    KIND_ACTION_PICKED,
    KIND_INTENT_REDIRECTED,
    KIND_CONTEXT_REDIRECTED,
    KIND_ACTION_REDIRECTED,
    KIND_REVIEW_REDIRECTED,
    KIND_ACTION_APPROVED,
    KIND_ACTION_REJECTED,
    KIND_EXECUTION_STARTED,
    KIND_EXECUTION_FINISHED,
    KIND_CONTEXT_ADDED,
    KIND_CONTEXT_REMOVED,
    KIND_SUBTHREADS_SPAWNED,
    KIND_SUBTHREAD_TERMINAL_REPORTED,
    KIND_PARENT_FORCE_CLOSE,
    KIND_ITEM_MOVED,
    KIND_GROUPS_SPAWNED,
    KIND_CONTEXT_ITEM_MOVED,
    KIND_GROUP_DELETED,
    KIND_THREAD_RENAMED,
    KIND_BUDGET_WARNING,
    KIND_LOOP_DETECTED,
    KIND_ESCALATED_TO_USER,
    KIND_STATE_TRANSITION,
    KIND_LATER,
    KIND_SOURCE_CLEANED_UP,
    KIND_CLEANUP_FAILED,
    KIND_AUTO_ADVANCE_DECISION,
    KIND_COMBINED_INFERRED_META,
})


# ---------------------------------------------------------------------------
# Actor labels
# ---------------------------------------------------------------------------

ACTOR_AGENT = "agent"
ACTOR_USER = "user"
ACTOR_SIDECAR = "sidecar"
ACTOR_FSM_ENGINE = "fsm_engine"
ACTOR_CONDUCTOR = "conductor"
ACTOR_INCITING = "inciting"


# ---------------------------------------------------------------------------
# ThreadEvent dataclass
# ---------------------------------------------------------------------------


@dataclass
class ThreadEvent:
    """A single entry in a Thread's append-only event log.

    Persisted to ``thread_events`` table (Stage 1.3 schema). The
    ``id`` field comes from the DB AUTOINCREMENT and is None on
    in-memory events that haven't been written yet.

    Optimistic locking: when an actor decides what to do next, it
    records the latest event ID it saw as ``parent_event_id`` on the
    next event it submits. The store rejects the insert if a newer
    event has landed (DESIGN.md §13.3).
    """

    thread_id: str
    kind: str  # one of ALL_KINDS (validated at submit)
    actor: str
    data: dict[str, Any] = field(default_factory=dict)
    parent_event_id: Optional[int] = None
    migration_id: Optional[str] = None  # links cross-Thread events
    inference_tier: Optional[str] = None  # ReasoningTier value if applicable
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    id: Optional[int] = None  # DB-assigned

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "kind": self.kind,
            "actor": self.actor,
            "data": self.data,
            "parent_event_id": self.parent_event_id,
            "migration_id": self.migration_id,
            "inference_tier": self.inference_tier,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ThreadEvent:
        data_raw = row.get("data_json") or row.get("data")
        if isinstance(data_raw, str) and data_raw:
            try:
                data = json.loads(data_raw)
            except json.JSONDecodeError:
                data = {}
        elif isinstance(data_raw, dict):
            data = data_raw
        else:
            data = {}

        return cls(
            id=row.get("id"),
            thread_id=row["thread_id"],
            kind=row["kind"],
            actor=row["actor"],
            data=data,
            parent_event_id=row.get("parent_event_id"),
            migration_id=row.get("migration_id"),
            inference_tier=row.get("inference_tier"),
            timestamp=row.get("timestamp") or "",
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_kind(kind: str) -> None:
    """Raise ValueError if ``kind`` is not in the canonical catalog."""
    if kind not in ALL_KINDS:
        raise ValueError(
            f"Unknown event kind: {kind!r}. "
            f"Add to events.ALL_KINDS or use an existing kind."
        )


# ---------------------------------------------------------------------------
# Optimistic-lock conflict
# ---------------------------------------------------------------------------


class OptimisticLockConflict(RuntimeError):
    """Raised when a Thread's latest event has changed under us.

    The submitter's ``parent_event_id`` no longer matches the most
    recent landed event. Re-read state and retry. See DESIGN.md §13.3.
    """
