"""Enums for the v5 Thread system.

These are the canonical names. Every other module references them by
import; literal strings should NOT be sprinkled around the codebase.
The values match DESIGN.md (data/designs/gtd/reimagined/DESIGN.md)
exactly.

Stage 1: types only, no behavior. Frozen signatures so downstream
stages can program against them.
"""

from __future__ import annotations

from enum import Enum


class FSMState(str, Enum):
    """The 14 resolution-phase FSM states.

    See DESIGN.md §7.2 for the catalog and DESIGN.md §7.6 for the
    transition table.
    """

    # Inception
    PROPOSED = "proposed"

    # Queued for inference (work lives in the LLM-call priority queue)
    AWAITING_INFERENCE = "awaiting_inference"

    # Inference running
    INFERRING_INTENT = "inferring_intent"
    INFERRING_CONTEXT = "inferring_context"
    INFERRING_ACTION = "inferring_action"

    # Resolution wait states
    AWAITING_INTENT_CONFIRMATION = "awaiting_intent_confirmation"
    AWAITING_INTENT_CLARIFICATION = "awaiting_intent_clarification"
    AWAITING_CONTEXT_CONFIRMATION = "awaiting_context_confirmation"
    AWAITING_CONTEXT_CLARIFICATION = "awaiting_context_clarification"
    AWAITING_ACTION_CLARIFICATION = "awaiting_action_clarification"
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # action gate
    AWAITING_REVIEW = "awaiting_review"  # post-execution, opt-in
    AWAITING_REDIRECT = "awaiting_redirect"

    # Execution
    EXECUTING = "executing"

    # Terminal
    DONE = "done"
    DISMISSED = "dismissed"
    HANDED_OFF = "handed_off"
    MONITORING = "monitoring"  # parent-of-decomposed

    # Stage 4: cleanup states (UX.md §6.4)
    CLEANING_UP = "cleaning_up"
    DONE_CLEANUP_SUCCESSFUL = "done_cleanup_successful"
    DONE_CLEANUP_UNSUCCESSFUL = "done_cleanup_unsuccessful"  # wait state

    @property
    def is_terminal(self) -> bool:
        return self in {
            FSMState.DONE,
            FSMState.DISMISSED,
            FSMState.HANDED_OFF,
            FSMState.DONE_CLEANUP_SUCCESSFUL,
        }

    @property
    def is_wait_state(self) -> bool:
        """True for any state where the FSM is paused for user input."""
        return self in {
            FSMState.AWAITING_INTENT_CONFIRMATION,
            FSMState.AWAITING_INTENT_CLARIFICATION,
            FSMState.AWAITING_CONTEXT_CONFIRMATION,
            FSMState.AWAITING_CONTEXT_CLARIFICATION,
            FSMState.AWAITING_ACTION_CLARIFICATION,
            FSMState.AWAITING_CONFIRMATION,
            FSMState.AWAITING_REVIEW,
            FSMState.AWAITING_REDIRECT,
            FSMState.DONE_CLEANUP_UNSUCCESSFUL,  # waits for retry/accept
        }

    @property
    def is_confirmation_state(self) -> bool:
        """Wait state where the agent has a guess; user reviews."""
        return self in {
            FSMState.AWAITING_INTENT_CONFIRMATION,
            FSMState.AWAITING_CONTEXT_CONFIRMATION,
            FSMState.AWAITING_CONFIRMATION,
        }

    @property
    def is_clarification_state(self) -> bool:
        """Wait state where the agent has nothing; user provides."""
        return self in {
            FSMState.AWAITING_INTENT_CLARIFICATION,
            FSMState.AWAITING_CONTEXT_CLARIFICATION,
            FSMState.AWAITING_ACTION_CLARIFICATION,
        }

    @property
    def is_inferring(self) -> bool:
        return self in {
            FSMState.INFERRING_INTENT,
            FSMState.INFERRING_CONTEXT,
            FSMState.INFERRING_ACTION,
        }


class InferenceTarget(str, Enum):
    """Routing target for the unified Inference class.

    See DESIGN.md §9.1 — one entry point parameterized by target,
    not three modules.
    """

    INTENT = "intent"
    CONTEXT = "context"
    ACTION = "action"
    AUTONOMY = "autonomy"  # forward-looking; not used in Stage 2 bootstrap
    # Stage 5 (combined inference): a single LLM call that returns
    # intent + context + action together. Used when the source
    # pipeline or per-thread autonomy axis opts in. The worker
    # records three separate *_inferred events from one COMBINED
    # call so the FSM and audit log stay shaped the same way as
    # staged inference; a follow-up `combined_inferred_meta` event
    # records that they all came from a single call.
    COMBINED = "combined"


class ReasoningTier(str, Enum):
    """The reasoning-tier ladder.

    The first 5 are existing tiers from work_buddy/llm/tiers.py and are
    reused. The last 2 (AGENT_HEADLESS, USER) are new in v5.

    Values are lowercase to match the strings used by
    ``work_buddy.llm.tiers.ModelTier`` (so cost-log entries, queue
    rows, and event ``inference_tier`` annotations interop without
    case-conversion).

    See DESIGN.md §9.2.
    """

    LOCAL_TOOL_CALLING = "local_tool_calling"
    LOCAL_FAST = "local_fast"
    FRONTIER_FAST = "frontier_fast"
    FRONTIER_BALANCED = "frontier_balanced"
    FRONTIER_BEST = "frontier_best"
    AGENT_HEADLESS = "agent_headless"  # multi-turn agent w/ tools (v5)
    USER = "user"                       # human-in-the-loop (v5)


class InvocationContext(str, Enum):
    """Where a capability/workflow may be discovered or invoked.

    See DESIGN.md §10.3. The gateway derives the caller's context
    server-side from session metadata; callers do NOT pass it.
    """

    AGENT_CONVERSATION = "agent_conversation"   # agent + user attending
    AGENT_AUTONOMOUS = "agent_autonomous"       # sidecar worker, no user
    FSM_INTERNAL = "fsm_internal"               # FSM engine state ops
    ACTION_PROPOSAL = "action_proposal"         # inferred action candidate
    USER_INVOCATION = "user_invocation"         # user-initiated


class ActionKind(str, Enum):
    """The three action kinds the FSM can dispatch.

    See DESIGN.md §10.1 and §10.6/§10.7.
    """

    STANDARD = "standard"      # registered capability or workflow
    IMPROVISED = "improvised"  # agent runs wild; no template
    SUGGESTION = "suggestion"  # agent blocked on user-held context


class Authorship(str, Enum):
    """Authorship enum carried through from PR #70 (slice-7 cleanup).

    Used on sub-Threads (formerly ActionItems) to gate executability.
    A row is executable iff authorship in {USER, AGENT_APPROVED} AND
    state is non-terminal. Defaults to AGENT_UNAPPROVED for safety.
    """

    USER = "user"
    AGENT_APPROVED = "agent_approved"
    AGENT_UNAPPROVED = "agent_unapproved"


class SurfaceUrgency(str, Enum):
    """Per-Resolution-Request surface urgency.

    See DESIGN.md §15.2.
    """

    DEFER = "defer"            # Now feed
    SURFACE_NOW = "surface_now"  # immediate notification


# ---------------------------------------------------------------------------
# Tier ordering for escalation comparisons
# ---------------------------------------------------------------------------

_TIER_ORDER: tuple[ReasoningTier, ...] = (
    ReasoningTier.LOCAL_TOOL_CALLING,
    ReasoningTier.LOCAL_FAST,
    ReasoningTier.FRONTIER_FAST,
    ReasoningTier.FRONTIER_BALANCED,
    ReasoningTier.FRONTIER_BEST,
    ReasoningTier.AGENT_HEADLESS,
    ReasoningTier.USER,
)


def tier_rank(tier: ReasoningTier) -> int:
    """Return a 0-based ordinal for a reasoning tier (cheaper = lower)."""
    return _TIER_ORDER.index(tier)


def tier_at_or_above(a: ReasoningTier, b: ReasoningTier) -> bool:
    """True iff tier ``a`` is at least as expensive/capable as ``b``."""
    return tier_rank(a) >= tier_rank(b)
