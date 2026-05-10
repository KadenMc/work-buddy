"""bootstrap — wire all FSM state-entry handlers and
register the budget admission hook.

Sidecar startup calls :func:`bootstrap_threads` once at the start of
the process. Tests call it (or its constituent pieces) explicitly
in fixtures. Idempotent — safe to call multiple times in a
process; the FSM-engine handler list is additive but the
behaviour is convergent (publishing the same notification ID
replaces the existing card).

What gets wired
---------------

1. **AWAITING_INFERENCE** → enqueue inference into the LLM-call
   queue (``inference_worker.awaiting_inference_handler``).
2. **All wait states** (``awaiting_*_confirmation``,
   ``awaiting_*_clarification``, ``awaiting_confirmation``,
   ``awaiting_review``, ``awaiting_redirect``) → publish a
   Resolution Surface card via the notifications subsystem
   (``resolution_surface._state_entry_handler``).
3. **Terminal states** (DONE, DISMISSED, HANDED_OFF) → cascade
   any parent thread's MONITORING → DONE check
   (``decompose.cascade_handler``).
4. **LLM-call queue admission hook** → per-caller budget check
   (``budget.budget_admission_hook``). For Thread callers, the
   budget is read from the Thread's autonomy_policy.budget_usd
   automatically (no explicit set_caller_budget needed).

Tests check the wiring count by snapshotting the handler maps
before/after.
"""

from __future__ import annotations

import logging
from typing import Optional

from work_buddy.llm import budget, queue
from work_buddy.threads import (
    cleanup_adapters,
    cleanup_runner,
    decompose,
    engine,
    inference_worker,
    resolution_surface,
)

logger = logging.getLogger(__name__)


_BOOTSTRAPPED = False


def is_bootstrapped() -> bool:
    return _BOOTSTRAPPED


def bootstrap_threads(*, clear_first: bool = False) -> None:
    """Wire all Threads-FSM state-entry handlers + budget admission.

    Parameters
    ----------
    clear_first:
        If True, clears all previously-registered FSM state-entry
        handlers AND admission hooks before wiring. Useful for
        tests; production startup keeps the default False so a
        re-bootstrap (e.g., after a config reload) doesn't lose
        third-party-registered handlers.

    Side effects
    ------------
    - engine.register_state_entry_handler(...) for every state.
    - queue.register_admission_hook(budget.budget_admission_hook).
    """
    global _BOOTSTRAPPED

    if clear_first:
        engine.clear_state_entry_handlers()
        queue.clear_admission_hooks()
        _BOOTSTRAPPED = False

    # 1. AWAITING_INFERENCE → enqueue
    inference_worker.register_inference_dispatch_handler()

    # 2. Every wait state → publish Resolution Surface card
    resolution_surface.register_resolution_surface_handlers()

    # 3. Terminal states → cascade to parent
    decompose.register_cascade_handlers()

    # 4. LLM-call queue admission hook
    queue.register_admission_hook(budget.budget_admission_hook)

    # 5. CLEANING_UP state-entry handler (Stage 4.4)
    cleanup_runner.register_cleanup_runner()

    # 5b. EXECUTING state-entry handler — dispatches the chosen action
    # capability, records execution_started/finished, fires the result
    # trigger to advance the FSM.
    from work_buddy.threads import execution_runner
    execution_runner.register_execution_runner()

    # 6. Default cleanup adapters (journal-note for Stage 4.4;
    #    chrome adapter lands in 4.13 alongside the pipeline).
    cleanup_adapters.register_default_adapters()

    # 7. Real LLM runner (replaces the Stage-2 stub). Without this,
    # inference workers would call _stub_runner and write empty
    # proposals with confidence=0 — the FSM advances but every
    # thread looks "the agent had nothing."
    _register_real_llm_runner()

    # 8. Wave D (2026-05-03): emit a thread.state_changed event
    # to the dashboard event bus on every FSM transition. The
    # dashboard frontend's threads-tab handler invalidates its
    # cache and re-renders, so the user sees fresh state without
    # manual refresh.
    _register_dashboard_event_emitter()

    _BOOTSTRAPPED = True
    logger.info("threads bootstrap complete")


def _register_dashboard_event_emitter() -> None:
    """Hook every FSM state-entry to publish a ``thread.state_changed``
    event on the dashboard event bus.

    Per process: the dashboard process publishes in-process (zero
    IPC); the sidecar publishes via the messaging-service bridge.
    Both go through ``publish_auto`` which picks the right channel.

    Best-effort: failures are logged but never block the FSM
    transition. The subscriber on the frontend invalidates its
    cache and re-renders the threads list.
    """
    try:
        from work_buddy.dashboard import events as bus
        from work_buddy.threads.enums import FSMState
    except Exception as e:
        logger.warning(
            "dashboard event emitter not registered: %s "
            "(threads will land but the dashboard won't auto-refresh)",
            e,
        )
        return

    def _emit(transition_result) -> None:
        try:
            bus.publish_auto(
                "thread.state_changed",
                {
                    "thread_id": transition_result.thread_id,
                    "prev_state": transition_result.prev_state.value,
                    "next_state": transition_result.next_state.value,
                    "trigger": transition_result.trigger,
                },
            )
        except Exception as ex:
            logger.debug(
                "thread.state_changed emit failed for %s: %s",
                transition_result.thread_id, ex,
            )

    # Register on every FSMState — we want both wait and active
    # states to fire updates so the dashboard sees mid-process
    # transitions when the toggle is on.
    for state in FSMState:
        engine.register_state_entry_handler(state, _emit)


def _normalize_parameters_json(payload: dict) -> None:
    """In-place convert ``parameters_json`` (string) to ``parameters``
    (dict) on action proposals.

    Anthropic's structured-output validator requires
    ``additionalProperties: false`` on every nested object schema,
    but action parameters are open-shape (each Standard Action
    declares its own parameter schema, agent-improvised actions can
    make up any keys). To preserve open-shape semantics we have the
    schema declare ``parameters_json: string`` and the agent
    serializes parameters as JSON. This helper parses the string
    back to a dict so downstream consumers see ``parameters``
    unchanged.

    Handles two payload shapes:
    - Staged ACTION inference: ``payload["parameters_json"]``.
    - COMBINED inference: ``payload["action"]["parameters_json"]``.

    Failures parse to an empty dict and are logged at debug level —
    we never raise from a normalization step that's supposed to be
    transparent.
    """
    import json as _json

    def _parse(holder: dict) -> None:
        raw = holder.pop("parameters_json", None)
        if raw is None:
            return
        try:
            parsed = _json.loads(raw) if raw else {}
            if not isinstance(parsed, dict):
                parsed = {}
        except (TypeError, ValueError):
            parsed = {}
        holder["parameters"] = parsed

    _parse(payload)
    nested_action = payload.get("action")
    if isinstance(nested_action, dict):
        _parse(nested_action)


def _is_action_schema(schema: dict) -> bool:
    """True iff the schema's ``kind`` enum lists action kinds.

    Both staged ACTION and COMBINED carry a 'kind' field whose enum
    includes 'standard' (and the others). Detecting via the schema
    avoids piping a target marker through the runner contract.
    """
    if not isinstance(schema, dict):
        return False
    props = schema.get("properties") or {}
    # Staged ACTION: 'kind' is at the top level
    kind_field = props.get("kind")
    if isinstance(kind_field, dict) and "standard" in (kind_field.get("enum") or []):
        return True
    # COMBINED: 'kind' lives under properties.action.properties.kind
    action = props.get("action")
    if isinstance(action, dict):
        action_props = action.get("properties") or {}
        action_kind = action_props.get("kind")
        if isinstance(action_kind, dict) and "standard" in (action_kind.get("enum") or []):
            return True
    return False


def _maybe_format_action_catalog(schema: dict) -> str:
    """Render the Standard Action catalog as a markdown block.

    Only emits when the schema is action-shaped (see
    ``_is_action_schema``); otherwise returns "" so the caller can
    blindly concatenate it into the user prompt.

    The block lists each action's name, one-line description, and
    the names of its parameters so the agent can pick by name and
    fill ``parameters_json`` correctly.
    """
    if not _is_action_schema(schema):
        return ""
    try:
        from work_buddy.threads import actions as _actions
        from work_buddy.threads.enums import InvocationContext
        catalog = _actions.catalog_for(InvocationContext.ACTION_PROPOSAL)
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("Action catalog format failed: %s", e)
        return ""
    if not catalog:
        return (
            "Available Standard Actions: (none registered — use "
            "improvised, suggestion, or clarification)\n\n"
        )
    lines = ["Available Standard Actions (pick one of these by name "
             "for kind='standard'; otherwise pick improvised, "
             "suggestion, or clarification):"]
    for tmpl in catalog:
        # Compact one-line description (truncate aggressively — the
        # full description is in the registry; the agent just needs
        # enough to match intent).
        desc = (tmpl.description or "").split(". ")[0][:160]
        params = list(tmpl.parameters.keys())
        param_list = ", ".join(params[:6]) if params else "(no params)"
        if len(params) > 6:
            param_list += ", ..."
        lines.append(f"- {tmpl.name}: {desc}")
        lines.append(f"    params: {param_list}")
    lines.append("")
    return "\n".join(lines) + "\n"


def _build_redirect_feedback_block(thread) -> str:
    """Phase 5: surface unresolved per-action redirect feedback.

    Reads the thread's recent events for ``KIND_ACTION_REDIRECTED``
    that landed AFTER the most recent ``KIND_ACTION_INFERRED``. If
    one exists, the user has asked us to re-propose this action with
    steering feedback — return a block describing what they wanted
    different so the LLM produces a meaningfully different proposal.

    Returns "" when no fresh redirect feedback applies (the common
    case — first-time inference, or a redirect that's already been
    answered by a newer action_inferred event).
    """
    try:
        from work_buddy.threads import store
        from work_buddy.threads.events import (
            KIND_ACTION_INFERRED,
            KIND_ACTION_REDIRECTED,
        )
        events = store.list_events(
            thread.thread_id,
            kinds=[KIND_ACTION_INFERRED, KIND_ACTION_REDIRECTED],
        )
        if not events:
            return ""

        # Walk newest → oldest. We want the most recent ACTION_REDIRECTED
        # only if it lands AFTER the most recent ACTION_INFERRED (i.e.,
        # the redirect is unresolved). If a newer ACTION_INFERRED has
        # already landed, the feedback was already incorporated — skip.
        latest_redirect = None
        latest_action_inferred = None
        for e in reversed(events):
            if e.kind == KIND_ACTION_REDIRECTED and latest_redirect is None:
                latest_redirect = e
            elif e.kind == KIND_ACTION_INFERRED and latest_action_inferred is None:
                latest_action_inferred = e
            if latest_redirect and latest_action_inferred:
                break

        if latest_redirect is None:
            return ""
        # Resolved? (A newer action_inferred has overwritten the redirect.)
        if (
            latest_action_inferred is not None
            and latest_redirect.id is not None
            and latest_action_inferred.id is not None
            and latest_action_inferred.id > latest_redirect.id
        ):
            return ""

        feedback = (latest_redirect.data or {}).get("feedback", "").strip()
        if not feedback:
            return ""

        prior_summary = ""
        if latest_action_inferred is not None:
            prior_payload = (latest_action_inferred.data or {}).get("payload") or {}
            prior_kind = prior_payload.get("kind", "?")
            prior_name = prior_payload.get("name") or prior_payload.get("plan_summary") or "(unnamed)"
            prior_params = prior_payload.get("parameters") or {}
            prior_summary = (
                f"\nPrior proposal (now superseded): kind={prior_kind}, "
                f"name={prior_name!r}, parameters={prior_params}\n"
            )

        return (
            "User redirect for this action — they asked for a different "
            "proposal than the prior one.\n"
            f"{prior_summary}"
            f"User feedback: {feedback!r}\n"
            "Re-propose the action accordingly. Keep the intent and "
            "context unchanged; only the action proposal needs to "
            "differ. If the feedback asks for a parameter tweak, "
            "respect it; if it asks for a different action entirely, "
            "pick one that better fits.\n\n"
        )
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("redirect-feedback block build failed: %s", e)
        return ""


def _register_real_llm_runner() -> None:
    """Bind the threads inference layer to the existing LLMRunner.

    Adapter shape (per inference.LLMRunnerFn):
        fn(prompt, schema, tier, thread) -> {payload, confidence,
                                              model, cost_usd,
                                              trace_pointer}
    """
    try:
        from work_buddy.threads import inference
        from work_buddy.threads.enums import ReasoningTier
        from work_buddy.llm import LLMRunner, ModelTier

        # Cache one LLMRunner instance — it's threadsafe.
        runner = LLMRunner()

        # Map ReasoningTier -> ModelTier. The lower 5 are 1:1;
        # AGENT_HEADLESS / USER are threads-only and shouldn't
        # reach this path (they're handled by the worker before
        # the LLM call).
        _TIER_MAP = {
            ReasoningTier.LOCAL_TOOL_CALLING: ModelTier.LOCAL_TOOL_CALLING,
            ReasoningTier.LOCAL_FAST: ModelTier.LOCAL_FAST,
            ReasoningTier.FRONTIER_FAST: ModelTier.FRONTIER_FAST,
            ReasoningTier.FRONTIER_BALANCED: ModelTier.FRONTIER_BALANCED,
            ReasoningTier.FRONTIER_BEST: ModelTier.FRONTIER_BEST,
        }

        def _real_runner(prompt, schema, tier, thread):
            """Adapter from inference.run() to LLMRunner.call()."""
            llm_tier = _TIER_MAP.get(tier, ModelTier.FRONTIER_FAST)
            # Build a minimal user prompt: prompt template + thread
            # context (inciting summary + intent if present).
            summary = thread.inciting_event_summary or {}
            # 2026-05-03: when this is an action prompt (the schema's
            # kind enum includes 'standard'), inject the Action Catalog
            # so the agent can actually pick a Standard Action by name
            # — previously the prompt said "pick from the Action
            # Catalog" but never showed what was in it, so the agent
            # consistently fell back to improvised/suggestion plans.
            catalog_block = _maybe_format_action_catalog(schema)

            # Phase 5: per-action redirect feedback. If the user has
            # asked us to re-infer THIS action with steering feedback
            # (KIND_ACTION_REDIRECTED event), surface it on the prompt
            # so the LLM produces a different proposal instead of
            # reproducing the same one. The prior action_inferred event
            # stays in history (render uses _latest to pick the new one);
            # we cite it here so the LLM understands what was rejected.
            redirect_block = _build_redirect_feedback_block(thread)

            user_msg = (
                "Thread inciting source:\n"
                f"  source: {summary.get('source')}\n"
                f"  description: {summary.get('description') or summary.get('label') or '(none)'}\n\n"
                "Context items: "
                f"{[ci.label for ci in thread.context_items]}\n\n"
                f"{catalog_block}"
                f"{redirect_block}"
                "Task:\n"
                f"{prompt}\n\n"
                "Reply with structured JSON matching the schema."
            )
            try:
                resp = runner.call(
                    tier=llm_tier,
                    system="You are an inference module for a task-management "
                           "system. Reply with concise structured JSON only.",
                    user=user_msg,
                    output_schema=schema,
                    trace_id=f"thread-inference:{thread.thread_id}",
                )
                if resp.is_error():
                    logger.warning(
                        "thread inference runner: error response: %s",
                        resp.content[:200],
                    )
                    return {
                        "payload": {}, "confidence": 0.0,
                        "model": None, "cost_usd": 0.0,
                        "trace_pointer": None,
                    }
                payload = resp.structured_output or {}
                # Action proposals carry parameters as a JSON string
                # (parameters_json) because Anthropic's structured-
                # output validator rejects open-shape ``object``
                # types. Parse it back to a dict so downstream
                # consumers (render data, autonomy_branch, action
                # dispatcher) see the canonical ``parameters`` shape.
                _normalize_parameters_json(payload)
                confidence = float(payload.get("confidence") or 0.0)
                cost = getattr(resp, "cost_usd", 0.0) or 0.0
                model = getattr(resp, "model_used", None)
                return {
                    "payload": payload,
                    "confidence": confidence,
                    "model": model,
                    "cost_usd": cost,
                    "trace_pointer": None,
                }
            except Exception as e:
                logger.warning("thread inference runner: exception: %s", e)
                return {
                    "payload": {}, "confidence": 0.0,
                    "model": None, "cost_usd": 0.0,
                    "trace_pointer": None,
                }

        inference.set_llm_runner(_real_runner)
        logger.info("thread inference runner registered (LLMRunner-backed)")
    except Exception as e:
        logger.warning(
            "Could not register real LLM runner — inference will use "
            "the stub (returns empty proposals). Reason: %s", e,
        )


def bootstrap_for_subprocess(*, subprocess_name: str) -> bool:
    """One-call bootstrap helper for any subprocess that may
    spawn or transition Threads.

    Each Python subprocess (sidecar daemon, dashboard, MCP gateway,
    one-off CLI invocations, …) has its own module-level state, so
    every process that fires FSM transitions needs its own
    ``bootstrap_threads()`` call to register state-entry handlers + the
    real LLM runner. Without this, transitions land in-memory but
    the queue handlers never fire and threads dead-end.

    This helper consolidates the boilerplate (try/except, logging)
    so every subprocess can call a single one-liner at startup.

    Args:
        subprocess_name: Logged for diagnostic visibility — appears
            in startup logs as e.g. "threads bootstrap (sidecar)".

    Returns:
        True if bootstrap succeeded, False on failure (logged).
        Callers may continue regardless; the threads FSM just won't
        process Threads in the failed subprocess.
    """
    try:
        bootstrap_threads()
        logger.info("threads bootstrap (%s) complete", subprocess_name)
        return True
    except Exception as e:
        logger.warning(
            "threads bootstrap failed in %s subprocess; that process "
            "will continue without thread FSM wiring: %s",
            subprocess_name, e,
        )
        return False


def teardown_threads() -> None:
    """Test-only: clear all state-entry handlers + admission hooks
    + cleanup adapters so the next test starts from a clean slate."""
    global _BOOTSTRAPPED
    engine.clear_state_entry_handlers()
    queue.clear_admission_hooks()
    from work_buddy.threads.cleanup import clear_cleanup_adapters
    from work_buddy.threads import inference
    clear_cleanup_adapters()
    inference.reset_llm_runner()
    # Reset budget cost sources too — tests that inject a fake
    # cumulative cost (e.g. "what if cumulative=$99?") leak their
    # override into the process-global state otherwise.
    budget.reset_cost_sources()
    budget.clear_caller_budgets()
    _BOOTSTRAPPED = False
