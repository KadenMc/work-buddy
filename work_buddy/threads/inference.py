"""Inference layer — one entry point parameterized by target.

Stage 2.3 deliverable. DESIGN.md §9.1 explicitly rejects the
"three modules" framing; inference is **one class** with a ``target``
parameter. Routing by target picks the prompt template, output
schema, default tier, and the event kind to record.

Adding a new inference target is a new entry in ``TARGETS`` plus a
prompt + schema. Not a new class.

Stage 2.3 ships:
- The class + target registry
- A pluggable LLM runner (so tests can stub without the full LLM
  stack online)
- ``*_inferred`` event recording with full provenance (target,
  tier, model, confidence, cost)
- Stub prompts / schemas for intent / context / action — Stage 2
  refines them as the actual inference behavior matures.

The Inference class does NOT directly enqueue into the LLM-call
queue; that's the job of the FSM engine when it transitions a
Thread to AWAITING_INFERENCE. The ``Inference.run()`` method is
called by the *worker* that has already dequeued, after the queue
has admitted the request. See Stage 2.4 (sidecar inference worker).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from work_buddy.threads import store
from work_buddy.threads.enums import (
    InferenceTarget,
    ReasoningTier,
)
from work_buddy.threads.events import (
    ACTOR_AGENT,
    KIND_ACTION_INFERRED,
    KIND_CONTEXT_INFERRED,
    KIND_INTENT_INFERRED,
    ThreadEvent,
)
from work_buddy.threads.models import Proposal, Thread

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Target registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetSpec:
    """Per-target configuration for the Inference router."""

    target: InferenceTarget
    event_kind: str                    # one of work_buddy.threads.events.KIND_*
    default_tier: ReasoningTier
    prompt_template: str               # caller-overridable; stub here
    output_schema: dict[str, Any]      # caller-overridable; stub here


# Default registry — Stage 2 ships stubs; refinement is per-target
# tuning work that lands as use cases come online.
TARGETS: dict[InferenceTarget, TargetSpec] = {
    InferenceTarget.INTENT: TargetSpec(
        target=InferenceTarget.INTENT,
        event_kind=KIND_INTENT_INFERRED,
        default_tier=ReasoningTier.FRONTIER_FAST,
        prompt_template=(
            "Given the Thread's inciting context and event log, "
            "infer the user's intent. Return a single concise "
            "phrase describing what the user is trying to accomplish."
        ),
        output_schema={
            "type": "object",
            "required": ["intent", "confidence"],
            "properties": {
                "intent": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "supporting_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
    ),
    InferenceTarget.CONTEXT: TargetSpec(
        target=InferenceTarget.CONTEXT,
        event_kind=KIND_CONTEXT_INFERRED,
        default_tier=ReasoningTier.FRONTIER_FAST,
        prompt_template=(
            "Given the Thread's inciting event and stated intent, "
            "infer the relevant context items (vault notes, Chrome "
            "tabs, calendar events, contracts) that should be "
            "associated with this Thread."
        ),
        output_schema={
            "type": "object",
            "required": ["associated_refs", "confidence"],
            "properties": {
                "associated_refs": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "reasoning": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    ),
    InferenceTarget.ACTION: TargetSpec(
        target=InferenceTarget.ACTION,
        event_kind=KIND_ACTION_INFERRED,
        default_tier=ReasoningTier.FRONTIER_BALANCED,
        prompt_template=(
            "Given the Thread's intent and context, propose the next "
            "action. Pick a Standard Action from the Action Catalog "
            "if one fits; otherwise produce an Improvised plan or a "
            "Suggestion if the agent is blocked on user-held context."
        ),
        output_schema={
            "type": "object",
            "required": ["kind", "confidence"],
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["standard", "improvised", "suggestion"],
                },
                "name": {"type": "string"},
                "parameters": {"type": "object"},
                "plan_summary": {"type": "string"},
                "rationale": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "blocked_on": {"type": "string"},
            },
        },
    ),
}


def register_target(spec: TargetSpec) -> None:
    """Add or replace a target spec. Stage 2.x as targets grow."""
    TARGETS[spec.target] = spec


# ---------------------------------------------------------------------------
# Pluggable LLM runner
# ---------------------------------------------------------------------------
#
# The runner is a callable:
#
#     fn(prompt: str, schema: dict, tier: ReasoningTier,
#        thread: Thread) -> dict
#         returns: {
#           "payload": <validated against schema>,
#           "confidence": float,
#           "model": str,
#           "cost_usd": float,
#           "trace_pointer": str | None,
#         }
#
# Stage 2 wires a real runner backed by work_buddy.llm.runner_v2.
# Stage 2.3 ships the contract + a stub for tests.
# ---------------------------------------------------------------------------


LLMRunnerFn = Callable[[str, dict, ReasoningTier, Thread], dict]


def _stub_runner(
    prompt: str, schema: dict, tier: ReasoningTier, thread: Thread,
) -> dict:
    """Default no-op runner. Returns a deterministic empty proposal
    with zero confidence so callers can wire structure without an
    LLM endpoint. Real runner registered via ``set_llm_runner``."""
    logger.info(
        "[stub] would run %s tier inference on %s; "
        "no real LLM runner registered",
        tier.value, thread.thread_id,
    )
    return {
        "payload": {},
        "confidence": 0.0,
        "model": None,
        "cost_usd": 0.0,
        "trace_pointer": None,
    }


_RUNNER: LLMRunnerFn = _stub_runner


def set_llm_runner(fn: LLMRunnerFn) -> None:
    """Register the LLM runner used by ``Inference.run()``.

    Stage 2 bootstrap registers a runner backed by
    ``work_buddy.llm.runner_v2``. Tests register stubs.
    """
    global _RUNNER
    _RUNNER = fn


def get_llm_runner() -> LLMRunnerFn:
    return _RUNNER


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnknownTarget(KeyError):
    """No spec registered for the requested target."""


# ---------------------------------------------------------------------------
# The Inference class
# ---------------------------------------------------------------------------


@dataclass
class Inference:
    """One entry point, parameterized by target.

    Holds an optional override map of target → TargetSpec so tests
    or callers can tune individual targets without mutating the
    module-global TARGETS dict.
    """

    overrides: dict[InferenceTarget, TargetSpec] = field(default_factory=dict)

    def get_spec(self, target: InferenceTarget) -> TargetSpec:
        spec = self.overrides.get(target) or TARGETS.get(target)
        if spec is None:
            raise UnknownTarget(
                f"No TargetSpec registered for {target!r}",
            )
        return spec

    def run(
        self,
        thread: Thread,
        target: InferenceTarget,
        *,
        tier: Optional[ReasoningTier] = None,
        record_event: bool = True,
        runner: Optional[LLMRunnerFn] = None,
        conn=None,
    ) -> Proposal:
        """Run inference for ``target`` against ``thread``.

        Parameters
        ----------
        tier: optional override; falls back to spec.default_tier.
        record_event: if False, don't write a ``*_inferred`` event
            (callers managing their own event semantics).
        runner: per-call runner override (test convenience).
        conn: optional shared SQLite connection.

        Returns a :class:`Proposal` with full provenance.
        """
        spec = self.get_spec(target)
        chosen_tier = tier or spec.default_tier
        run_fn = runner or _RUNNER

        # Run the (pluggable) LLM call
        out = run_fn(
            spec.prompt_template,
            spec.output_schema,
            chosen_tier,
            thread,
        )

        proposal = Proposal(
            target=spec.target.value,
            payload=out.get("payload") or {},
            confidence=float(out.get("confidence") or 0.0),
            tier_used=chosen_tier,
            model_used=out.get("model"),
            cost_usd=float(out.get("cost_usd") or 0.0),
            reasoning_trace_pointer=out.get("trace_pointer"),
        )

        if record_event:
            event = ThreadEvent(
                thread_id=thread.thread_id,
                kind=spec.event_kind,
                actor=ACTOR_AGENT,
                inference_tier=chosen_tier.value,
                data=proposal.to_dict(),
            )
            store.append_event(event, expect_parent_event_id=None, conn=conn)

        return proposal


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

_DEFAULT_INFERENCE = Inference()


def run(
    thread: Thread,
    target: InferenceTarget,
    *,
    tier: Optional[ReasoningTier] = None,
    record_event: bool = True,
    runner: Optional[LLMRunnerFn] = None,
    conn=None,
) -> Proposal:
    """Module-level shortcut around the default Inference instance."""
    return _DEFAULT_INFERENCE.run(
        thread, target,
        tier=tier, record_event=record_event,
        runner=runner, conn=conn,
    )
