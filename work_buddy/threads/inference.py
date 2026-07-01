"""Inference layer — one entry point parameterized by target.

DESIGN.md §9.1 explicitly rejects the
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


# Default registry — This module ships stubs; refinement is per-target
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
            "additionalProperties": False,
            "properties": {
                "intent": {"type": "string"},
                # Confidence is in [0, 1] by convention but we
                # don't constrain it in the schema — Anthropic's
                # structured-output schema validator rejects
                # minimum/maximum on number types. The runner
                # clamps the value at use time.
                "confidence": {"type": "number"},
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
            "additionalProperties": False,
            "properties": {
                "associated_refs": {
                    "type": "array",
                    # Items are ContextItem-shaped dicts. Anthropic's
                    # structured-output validator requires
                    # additionalProperties: false on EVERY nested
                    # object schema, recursively. We enumerate the
                    # expected fields and intentionally OMIT the
                    # source-specific ``payload`` (URL/line_text/etc.)
                    # — that's enriched downstream from the actual
                    # source registry by id+source. Anthropic
                    # rejected an open ``payload: {"type": "object"}``
                    # so we just don't ask the agent for it here.
                    # Discovered live 2026-05-03.
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "source", "type", "label"],
                        "properties": {
                            "id": {"type": "string"},
                            "source": {"type": "string"},
                            "type": {"type": "string"},
                            "label": {"type": "string"},
                        },
                    },
                },
                "reasoning": {"type": "string"},
                # Confidence is in [0, 1] by convention but we
                # don't constrain it in the schema — Anthropic's
                # structured-output schema validator rejects
                # minimum/maximum on number types. The runner
                # clamps the value at use time.
                "confidence": {"type": "number"},
            },
        },
    ),
    InferenceTarget.ACTION: TargetSpec(
        target=InferenceTarget.ACTION,
        event_kind=KIND_ACTION_INFERRED,
        default_tier=ReasoningTier.FRONTIER_BALANCED,
        prompt_template=(
            "Given the Thread's intent and context, propose the next "
            "action. Pick exactly one kind:\n"
            "- 'standard': a registered Action Catalog entry. STRONGLY "
            "PREFER this when an entry fits — e.g. a journal line "
            "marked 'wb/TODO X' is a literal request to create task "
            "X via the 'task_create' standard action, NOT a "
            "clarification.\n"
            "- 'improvised': you have a concrete plan that isn't a "
            "registered action. Provide a plan_summary describing "
            "what you'll do.\n"
            "- 'suggestion': you have a concrete advisory recommendation "
            "for the user (e.g. 'consider archiving this'). NOT for "
            "asking the user questions.\n"
            "- 'clarification': you genuinely cannot propose any "
            "action without more user input. Use the blocked_on field "
            "to state exactly what you need. Reserve this for cases "
            "where the inciting context is too sparse to map to any "
            "standard action — and remember 'wb/TODO X' alone IS "
            "enough to call task_create with task_text='X'.\n"
            "When you pick a 'standard' action, fill EVERY required "
            "parameter (marked '*' in the catalog) in parameters_json, "
            "inferring sensible values from the Thread's intent, context, "
            "and items. If a redirect block below supplies parameter "
            "values or a target action, keep those values verbatim and "
            "only fill the ones still missing."
        ),
        output_schema={
            "type": "object",
            "required": ["kind", "confidence"],
            "additionalProperties": False,
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": [
                        "standard", "improvised",
                        "suggestion", "clarification",
                    ],
                },
                "name": {"type": "string"},
                # parameters is action-specific — each Standard
                # Action declares its own parameter shape and we
                # don't validate them at the inference layer.
                # Anthropic's structured-output validator rejects
                # ``"type": "object"`` without ``additionalProperties:
                # false`` even on nested fields. To preserve the
                # open-shape semantics we encode parameters as a
                # JSON STRING and parse downstream. The agent
                # serializes its proposed parameters as JSON.
                # Discovered live 2026-05-03.
                "parameters_json": {"type": "string"},
                "plan_summary": {"type": "string"},
                "rationale": {"type": "string"},
                # Confidence is in [0, 1] by convention but we
                # don't constrain it in the schema — Anthropic's
                # structured-output schema validator rejects
                # minimum/maximum on number types. The runner
                # clamps the value at use time.
                "confidence": {"type": "number"},
                "blocked_on": {"type": "string"},
                # Risk metadata — declared by the agent for
                # improvised actions; standard actions override
                # via the ActionTemplate.intrinsic_amplifiers
                # registry. Read by autonomy_branch.resolve_action_branch.
                "irreversibility": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                },
                "regret_potential": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                },
                "risk_amplifier": {"type": "boolean"},
            },
        },
    ),
    # combined inference. Returns intent + context + action
    # in one LLM call. The worker records three separate *_inferred
    # events from the single call so the FSM and audit log shape
    # stays the same as staged inference; a follow-up
    # combined_inferred_meta event records that they all came from
    # one call. Default tier is FRONTIER_BALANCED — combined is more
    # demanding than each individual target and benefits from the
    # extra capability.
    InferenceTarget.COMBINED: TargetSpec(
        target=InferenceTarget.COMBINED,
        event_kind="combined_inferred",  # virtual; per-target events
                                          # are recorded separately
        default_tier=ReasoningTier.FRONTIER_BALANCED,
        prompt_template=(
            "Given the Thread's inciting context and event log, infer "
            "in one pass:\n"
            "1. The user's intent — a single concise phrase.\n"
            "2. The relevant context items (vault notes, Chrome tabs, "
            "calendar events, contracts).\n"
            "3. The next action to propose. Pick a Standard Action "
            "from the Action Catalog if one fits; otherwise produce "
            "an Improvised plan or a Suggestion. Declare the action's "
            "irreversibility and regret_potential ('low'|'medium'|'high') "
            "and risk_amplifier (true/false) so the autonomy layer can "
            "decide whether to auto-execute or surface for approval.\n"
            "Return all three in the structured response, with a "
            "single overall confidence."
        ),
        output_schema={
            "type": "object",
            "required": ["intent", "context", "action", "confidence"],
            "additionalProperties": False,
            "properties": {
                "intent": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["intent"],
                    "properties": {
                        "intent": {"type": "string"},
                        "supporting_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "context": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["associated_refs"],
                    "properties": {
                        "associated_refs": {
                            "type": "array",
                            # Same shape as the staged CONTEXT
                            # target — see comment there.
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["id", "source", "type", "label"],
                                "properties": {
                                    "id": {"type": "string"},
                                    "source": {"type": "string"},
                                    "type": {"type": "string"},
                                    "label": {"type": "string"},
                                },
                            },
                        },
                        "reasoning": {"type": "string"},
                    },
                },
                "action": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind"],
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "standard", "improvised",
                                "suggestion", "clarification",
                            ],
                        },
                        "name": {"type": "string"},
                        # See staged ACTION target's comment for why
                        # parameters is a JSON STRING here, not an
                        # open object schema.
                        "parameters_json": {"type": "string"},
                        "plan_summary": {"type": "string"},
                        "rationale": {"type": "string"},
                        "blocked_on": {"type": "string"},
                        "irreversibility": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "regret_potential": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "risk_amplifier": {"type": "boolean"},
                    },
                },
                "confidence": {"type": "number"},
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
# This module ships the contract + a stub for tests.
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


def reset_llm_runner() -> None:
    """Test-only: restore the stub runner. Used by ``teardown_threads``
    so a ``bootstrap_threads`` in one test doesn't leak its real
    runner into sibling tests in the same process."""
    global _RUNNER
    _RUNNER = _stub_runner


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
            # refresh search-blob whenever a new
            # *_inferred event lands. Best-effort — failure
            # is non-fatal (a stale search blob is degraded UX,
            # not a correctness issue).
            try:
                from work_buddy.threads.search import update_search_blob
                update_search_blob(thread.thread_id, conn=conn)
            except Exception as e:
                logger.warning(
                    "Search-blob refresh failed for %s: %s",
                    thread.thread_id, e,
                )

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
