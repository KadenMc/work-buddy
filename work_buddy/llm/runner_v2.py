"""Unified LLM runner — phase 1 skeleton.

One entry point (:class:`LLMRunner.call`) that accepts a semantic
:class:`ModelTier` and returns a normalized :class:`LLMResponse`.
Phase 1 routes internally to the existing :func:`run_task`
(structured output) and :func:`llm_with_tools` (not yet wired here —
deferred to the tool-call phase) plumbing without rewriting either;
this keeps the refactor safe to land before any callers migrate.

The value of phase 1 is the **API surface** and the **escalation loop**
— callers can start using ``LLMRunner`` today, and later phases swap
out the internal dispatch without touching caller code.

Scope / limitations for phase 1:

- ``tools=`` is accepted in the signature but raises
  :class:`NotImplementedError` if non-empty. Tool-call dispatch (both
  LM Studio server-side and Anthropic client-side) lands in phase 3
  when the first tool-using caller migrates.
- Only structured output via ``output_schema`` is supported on
  frontier tiers. Local tiers without ``output_schema`` fall through
  to profile-driven ``run_task`` — content-only.
- No streaming.
- No prompt caching knob yet (Anthropic-side caching stays implicit
  via the existing ``run_task`` cache).
"""

from __future__ import annotations

import time
from typing import Any

from work_buddy.llm.response import (
    ErrorKind,
    LLMResponse,
    TierAttempt,
    ToolCall,
)
from work_buddy.llm.tiers import ModelTier, TierBinding, resolve_tier, legacy_tier_for
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Error classification — map backend errors onto the normalized ErrorKind.
# ---------------------------------------------------------------------------

# LocalInferenceError.kind → ErrorKind. Unknown strings fall back to
# ErrorKind.UNKNOWN; this lets us absorb future kinds without crashing.
_LOCAL_ERROR_KIND_MAP: dict[str, ErrorKind] = {
    "timeout": ErrorKind.TIMEOUT,
    "mcp_gateway_timeout": ErrorKind.TIMEOUT,
    "mcp_fetch_failed": ErrorKind.BACKEND_UNAVAILABLE,
    "server_unreachable": ErrorKind.BACKEND_UNAVAILABLE,
    "lm_link_dropped": ErrorKind.BACKEND_UNAVAILABLE,
    "server_error": ErrorKind.UNKNOWN,
    "bad_request": ErrorKind.BAD_REQUEST,
    "context_exceeded": ErrorKind.CONTEXT_EXCEEDED,
    "model_not_available": ErrorKind.MODEL_NOT_AVAILABLE,
    "model_unsupported": ErrorKind.MODEL_UNSUPPORTED,
    "malformed_response": ErrorKind.MALFORMED_RESPONSE,
    "unknown": ErrorKind.UNKNOWN,
}


def _classify_error(error: str | None, kind_str: str | None) -> ErrorKind | None:
    """Best-effort classification of a backend error into :class:`ErrorKind`.

    Prefers the structured ``kind`` string when present; falls back to
    substring heuristics on the raw message. Returns ``None`` when
    there is no error to classify.
    """
    if not error and not kind_str:
        return None
    if kind_str and kind_str in _LOCAL_ERROR_KIND_MAP:
        return _LOCAL_ERROR_KIND_MAP[kind_str]
    # Heuristic fallback for errors from ``run_task`` which has a bare
    # ``error`` string with no kind discriminator.
    lower = (error or "").lower()
    if "context" in lower and ("exceed" in lower or "too long" in lower):
        return ErrorKind.CONTEXT_EXCEEDED
    if "timeout" in lower or "timed out" in lower:
        return ErrorKind.TIMEOUT
    if "rate limit" in lower or "429" in lower:
        return ErrorKind.RATE_LIMITED
    if "authentication" in lower or "api key" in lower or "401" in lower:
        return ErrorKind.AUTH
    if "schema" in lower or "json" in lower and "invalid" in lower:
        return ErrorKind.SCHEMA_VIOLATION
    return ErrorKind.UNKNOWN


def _detect_empty_content(
    content: str,
    structured: dict | None,
    tool_calls: tuple[ToolCall, ...],
) -> bool:
    """A completion is 'empty' when every observable output is missing.

    Used by the escalation check to catch the specific local-LLM failure
    mode where LM Studio returns 200 OK but no content, no structured
    output, and no tool calls — previously logged as ``content_len=0``
    by :class:`BackgroundTriageProducer`.
    """
    if content.strip():
        return False
    if structured is not None:
        return False
    if tool_calls:
        return False
    return True


def _normalize_escalate_on(
    conditions: list[ErrorKind | str] | None,
) -> set[ErrorKind]:
    """Accept a mix of ``ErrorKind`` enums and raw strings; return a set.

    Callers can pass either ``[ErrorKind.TIMEOUT, "empty_content"]`` or
    all one form; this normalization shields downstream code from the
    mix.
    """
    if not conditions:
        return set()
    out: set[ErrorKind] = set()
    for c in conditions:
        if isinstance(c, ErrorKind):
            out.add(c)
            continue
        try:
            out.add(ErrorKind(c))
        except ValueError:
            logger.warning("Unknown escalate_on condition %r; skipping", c)
    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class LLMRunner:
    """Single entry point to the LLM layer.

    Instantiate once per caller (cheap — state is config-only) and reuse::

        runner = LLMRunner()
        resp = runner.call(
            tier=ModelTier.FRONTIER_BALANCED,
            system=..., user=...,
            output_schema=SCHEMA,
            escalate_on=[ErrorKind.TIMEOUT, ErrorKind.CONTEXT_EXCEEDED],
            escalate_to=[ModelTier.FRONTIER_BEST],
        )
        if resp.is_error():
            ...
    """

    def call(
        self,
        *,
        tier: ModelTier,
        system: str,
        user: str,
        tools: list[str] | None = None,
        output_schema: dict | str | None = None,
        escalate_on: list[ErrorKind | str] | None = None,
        escalate_to: list[ModelTier] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        cache_ttl_minutes: int | None = None,
        trace_id: str | None = None,
    ) -> LLMResponse:
        """Execute one LLM call, escalating through fallback tiers on matching failures.

        Args:
            tier: Starting tier.
            system: System prompt.
            user: User prompt.
            tools: NOT IMPLEMENTED in phase 1 — raises if non-empty.
            output_schema: JSON Schema dict (or a path-string resolved by
                :func:`run_task`) for structured output. Optional.
            escalate_on: Error kinds that trigger an escalation attempt.
                Empty/None = no escalation regardless of outcome.
            escalate_to: Ordered list of fallback tiers, tried in order
                when escalation fires. Empty/None = no escalation.
            max_tokens, temperature: Override the tier defaults.
            cache_ttl_minutes: Passed through to :func:`run_task`.
            trace_id: Passed through for debug correlation.

        Returns:
            :class:`LLMResponse`. Check ``resp.is_error()`` before
            reading output fields.
        """
        if tools:
            raise NotImplementedError(
                "LLMRunner.call(tools=...) — tool-call dispatch lands in "
                "phase 3. Use output_schema for structured output, or call "
                "llm_with_tools directly until then."
            )

        escalate_kinds = _normalize_escalate_on(escalate_on)
        tier_chain = [tier] + list(escalate_to or [])
        attempts: list[TierAttempt] = []

        for attempt_tier in tier_chain:
            binding = resolve_tier(attempt_tier)
            t0 = time.time()
            resp = self._call_one(
                binding=binding,
                system=system,
                user=user,
                output_schema=output_schema,
                max_tokens=max_tokens if max_tokens is not None else binding.max_tokens,
                temperature=temperature if temperature is not None else binding.temperature,
                cache_ttl_minutes=cache_ttl_minutes,
                trace_id=trace_id,
            )
            elapsed_ms = int((time.time() - t0) * 1000)

            # Record this attempt regardless of outcome for audit.
            attempts.append(TierAttempt(
                tier=attempt_tier.value,
                model=resp.model,
                error_kind=resp.error_kind,
                error=resp.error,
                elapsed_ms=elapsed_ms,
            ))

            # Did this attempt succeed? Re-check empty content here so
            # ``empty_content`` triggers escalation even though the
            # backend returned 200 OK and no explicit error.
            if resp.error_kind is None and _detect_empty_content(
                resp.content, resp.structured_output, resp.tool_calls,
            ):
                resp = _with_error(resp, ErrorKind.EMPTY_CONTENT,
                                   "Backend returned no content, structured output, or tool calls.",
                                   "The model produced an empty completion. "
                                   "Raise max_tokens, narrow the prompt, or "
                                   "check the tier's context window.")
                # Also update the attempt record.
                attempts[-1] = TierAttempt(
                    tier=attempt_tier.value,
                    model=resp.model,
                    error_kind=ErrorKind.EMPTY_CONTENT,
                    error=resp.error,
                    elapsed_ms=elapsed_ms,
                )

            if resp.error_kind is None:
                # Success — return with the full audit trail.
                return _with_attempts(resp, attempts)

            # Failed. Escalate?
            if resp.error_kind in escalate_kinds and attempt_tier != tier_chain[-1]:
                logger.info(
                    "LLMRunner: %s failed with %s; escalating",
                    attempt_tier.value, resp.error_kind.value,
                )
                continue

            # No escalation — return the failure with the full trail.
            return _with_attempts(resp, attempts)

        # Every tier in the chain failed. Attach the trail to the last resp.
        return _with_attempts(resp, attempts)

    # -- internal -----------------------------------------------------------

    def _call_one(
        self,
        *,
        binding: TierBinding,
        system: str,
        user: str,
        output_schema: dict | str | None,
        max_tokens: int,
        temperature: float,
        cache_ttl_minutes: int | None,
        trace_id: str | None,
    ) -> LLMResponse:
        """Dispatch one attempt to the right backend adapter.

        Phase 1 implementation: thin wrapper over the existing
        :func:`run_task`. Local tiers use the profile path; frontier
        tiers translate through :func:`legacy_tier_for` to the legacy
        :class:`work_buddy.llm.runner.ModelTier`.
        """
        from work_buddy.llm.runner import ModelTier as LegacyTier
        from work_buddy.llm.runner import run_task

        legacy_tier_str = legacy_tier_for(binding.tier)

        kwargs: dict[str, Any] = {
            "task_id": trace_id or f"llm_call:{binding.tier.value}",
            "system": system,
            "user": user,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "output_schema": output_schema if isinstance(output_schema, dict) else None,
            "cache_ttl_minutes": cache_ttl_minutes,
            "trace_id": trace_id,
        }
        # If output_schema is a string, run_task resolves it itself —
        # pass through verbatim.
        if isinstance(output_schema, str):
            kwargs["output_schema"] = output_schema  # run_task accepts str path

        if binding.backend == "anthropic":
            if not legacy_tier_str:
                return _error_response(
                    binding,
                    ErrorKind.BACKEND_UNAVAILABLE,
                    f"Tier {binding.tier.value} has backend=anthropic but no legacy mapping",
                    "Add a legacy_tier_for mapping for this tier in tiers.py.",
                )
            kwargs["tier"] = LegacyTier(legacy_tier_str)
            if binding.model:
                kwargs["model"] = binding.model
        else:
            # Local path: run_task accepts ``profile=`` for openai_compat /
            # lmstudio_native. Tool-call dispatch via llm_with_tools is
            # deferred to phase 3.
            if not binding.profile:
                return _error_response(
                    binding,
                    ErrorKind.BACKEND_UNAVAILABLE,
                    f"Tier {binding.tier.value} has backend={binding.backend} but no profile",
                    "Set ``profile`` on the tier's config entry.",
                )
            # ``backend_kind`` tells run_task which local endpoint kind
            # to use. The tier binding is authoritative: tool-support
            # tiers hit the native MCP-capable endpoint, non-tool tiers
            # use openai-compat so LM Studio's JIT auto-load works.
            if binding.backend not in ("lmstudio_native", "openai_compat"):
                return _error_response(
                    binding,
                    ErrorKind.BACKEND_UNAVAILABLE,
                    f"Tier {binding.tier.value} has unsupported local "
                    f"backend={binding.backend!r}",
                    "Expected 'lmstudio_native' or 'openai_compat'.",
                )
            kwargs["profile"] = binding.profile
            kwargs["backend_kind"] = binding.backend

        try:
            result = run_task(**kwargs)
        except Exception as exc:
            logger.exception("LLMRunner: run_task raised")
            return _error_response(
                binding,
                ErrorKind.UNKNOWN,
                f"{type(exc).__name__}: {exc}",
                "",
            )

        # Normalize into LLMResponse.
        error_kind = _classify_error(result.error, None) if result.error else None

        return LLMResponse(
            content=result.content,
            structured_output=result.parsed,
            tool_calls=(),                     # phase 1 — tools not wired
            reasoning=None,                    # run_task doesn't surface reasoning
            reasoning_artifact_id=None,
            tier_used=binding.tier.value,
            tier_attempts=(),                  # filled by caller
            model=result.model,
            backend=binding.backend,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            reasoning_tokens=0,
            cost_usd=0.0,                      # phase 1 — cost passthrough later
            cached=result.cached,
            cache_key=result.cache_key,
            error=result.error,
            error_kind=error_kind,
            hint=None,
            backend_extra={},
        )


# ---------------------------------------------------------------------------
# Small helpers that rebuild frozen LLMResponse instances without
# pulling in dataclasses.replace for each field.
# ---------------------------------------------------------------------------


def _with_attempts(resp: LLMResponse, attempts: list[TierAttempt]) -> LLMResponse:
    from dataclasses import replace
    return replace(resp, tier_attempts=tuple(attempts))


def _with_error(
    resp: LLMResponse, kind: ErrorKind, message: str, hint: str,
) -> LLMResponse:
    from dataclasses import replace
    return replace(resp, error=message, error_kind=kind, hint=hint)


def _error_response(
    binding: TierBinding, kind: ErrorKind, message: str, hint: str,
) -> LLMResponse:
    return LLMResponse(
        tier_used=binding.tier.value,
        model=binding.model or binding.profile or "",
        backend=binding.backend,
        error=message,
        error_kind=kind,
        hint=hint,
    )


# ---------------------------------------------------------------------------
# Module-level convenience — the canonical import path for callers.
# ---------------------------------------------------------------------------

_default_runner = LLMRunner()


def llm_call(**kwargs: Any) -> LLMResponse:
    """Convenience wrapper around :meth:`LLMRunner.call`.

    Prefer ``from work_buddy.llm import llm_call; llm_call(...)`` over
    instantiating :class:`LLMRunner` yourself unless you need to hold
    state (you don't — the runner is config-only).

    NOTE: The existing ``work_buddy.llm.call.llm_call`` wrapper around
    :func:`run_task` retains its current signature during the phase 2–3
    migration. This function is importable as
    ``work_buddy.llm.runner_v2.llm_call`` today; it will be promoted to
    ``work_buddy.llm.llm_call`` once the legacy shim is retired.
    """
    return _default_runner.call(**kwargs)
