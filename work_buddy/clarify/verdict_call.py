"""Shared verdict-call helper with post-response validation escalation.

Both :mod:`work_buddy.clarify.capabilities.inline_triage_scan` and
:mod:`work_buddy.clarify.capabilities.journal_triage_scan` share an
identical call pattern against :class:`LLMRunner`:

  1. Ask for a constrained-JSON verdict, letting the runner's built-in
     tier escalation handle backend failures (TIMEOUT / EMPTY_CONTENT
     / CONTEXT_EXCEEDED / RATE_LIMITED).
  2. Validate the returned ``structured_output`` has the required
     semantic fields (notably ``recommended_action``).
  3. If validation fails at anything below ``FRONTIER_BEST``, re-call
     at ``FRONTIER_BEST`` once and validate again.

This module centralizes step 3 — the runner's built-in escalation only
fires on :class:`ErrorKind` failures, not on semantic validation of
parsed-but-incomplete structured output. Without this helper each
caller would need its own validation-escalation loop.

The resulting failure kind is :data:`ErrorKind.VALIDATION_FAILED` —
distinct from :data:`ErrorKind.SCHEMA_VIOLATION` because the content
DID parse against the JSON schema; the failure is semantic
(required-for-us field missing), not structural.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from work_buddy.llm import ErrorKind, LLMResponse, LLMRunner, ModelTier
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


_BACKEND_ESCALATE_ON: list[ErrorKind] = [
    ErrorKind.TIMEOUT,
    ErrorKind.CONTEXT_EXCEEDED,
    ErrorKind.EMPTY_CONTENT,
    ErrorKind.RATE_LIMITED,
]


def call_for_verdict(
    *,
    runner: LLMRunner,
    tier: ModelTier,
    system: str,
    user: str,
    output_schema: dict[str, Any],
    required_fields: tuple[str, ...] = ("recommended_action",),
    caller: str = "triage",
    item_id: str = "",
    trace_id: str | None = None,
) -> LLMResponse:
    """Run a verdict call with backend + validation escalation.

    Backend-failure escalation (TIMEOUT, EMPTY_CONTENT, ...) is handled
    by :class:`LLMRunner` internally at each attempt. Validation-failure
    escalation — the parsed JSON lacks one of ``required_fields`` — is
    handled here: if the responding tier was not ``FRONTIER_BEST``, one
    additional call is issued at ``FRONTIER_BEST``; if that also fails
    validation, the returned response carries
    :data:`ErrorKind.VALIDATION_FAILED`.

    Args:
        runner: Shared :class:`LLMRunner` instance.
        tier: Starting tier.
        system: System prompt.
        user: User prompt.
        output_schema: JSON Schema enforced at the backend.
        required_fields: Keys whose presence (non-empty) in
            ``structured_output`` defines "validation passed." Defaults
            to ``("recommended_action",)``.
        caller: Short tag for log correlation (``"journal_triage"`` etc.).
        item_id: Item id for log correlation. Empty when not applicable.

    Returns:
        The final :class:`LLMResponse`. On success ``is_error()`` is
        ``False`` and ``structured_output`` contains all required fields.
        On backend failure ``error_kind`` carries the backend's kind.
        On validation failure ``error_kind == ErrorKind.VALIDATION_FAILED``.
    """
    if trace_id is None:
        import uuid as _uuid
        suffix = _uuid.uuid4().hex[:8]
        trace_id = f"{caller}:{item_id}:{suffix}" if item_id else f"{caller}:{suffix}"

    adapter_attempts: list[dict[str, Any]] = []

    def _record(resp_obj: LLMResponse, outcome: str) -> None:
        adapter_attempts.append({
            "tier": resp_obj.tier_used or "",
            "model": resp_obj.model,
            "outcome": outcome,
            "error_kind": (resp_obj.error_kind.value
                           if resp_obj.error_kind else None),
            "error": resp_obj.error,
            "elapsed_ms": 0,  # timing of the LLMRunner call already logged separately
            "input_tokens": resp_obj.input_tokens,
            "output_tokens": resp_obj.output_tokens,
        })

    def _emit(final_outcome: str, final_tier: str) -> None:
        try:
            from work_buddy.llm.escalation_log import log_escalation
            log_escalation(
                source="verdict_call",
                attempts=adapter_attempts,
                final_outcome=final_outcome,
                final_tier=final_tier,
                trace_id=trace_id,
                task_id=f"{caller}:{item_id}" if item_id else caller,
                metadata={"required_fields": list(required_fields)},
            )
        except Exception:  # noqa: BLE001
            logger.debug("escalation log write skipped", exc_info=True)

    internal_esc_to: list[ModelTier] = (
        [ModelTier.FRONTIER_BEST] if tier != ModelTier.FRONTIER_BEST else []
    )
    resp = runner.call(
        tier=tier,
        system=system,
        user=user,
        output_schema=output_schema,
        escalate_on=_BACKEND_ESCALATE_ON,
        escalate_to=internal_esc_to,
        trace_id=trace_id,
    )
    if resp.is_error():
        _record(resp, "backend_error")
        _emit("backend_error", resp.tier_used or tier.value)
        return resp

    missing = _missing_fields(resp, required_fields)
    if not missing:
        _record(resp, "success")
        _emit("success", resp.tier_used or tier.value)
        return resp

    _record(resp, "validation_failed")
    used = resp.tier_used or tier.value
    if used == ModelTier.FRONTIER_BEST.value:
        # Already at top tier — no further escalation possible.
        logger.warning(
            "%s: verdict missing %s at final tier=%s (item=%s)",
            caller, missing, used, item_id,
        )
        _emit("validation_failed", used)
        return _as_validation_failed(resp, missing)

    logger.warning(
        "%s: verdict missing %s at tier=%s — escalating to FRONTIER_BEST "
        "for validation retry (item=%s)",
        caller, missing, used, item_id,
    )
    retry = runner.call(
        tier=ModelTier.FRONTIER_BEST,
        system=system,
        user=user,
        output_schema=output_schema,
        escalate_on=_BACKEND_ESCALATE_ON,
        escalate_to=[],
        trace_id=trace_id,
    )
    if retry.is_error():
        _record(retry, "backend_error")
        _emit("backend_error", retry.tier_used or ModelTier.FRONTIER_BEST.value)
        return retry

    retry_missing = _missing_fields(retry, required_fields)
    if not retry_missing:
        _record(retry, "success")
        _emit("success", retry.tier_used or ModelTier.FRONTIER_BEST.value)
        return retry

    _record(retry, "validation_failed")
    logger.warning(
        "%s: verdict missing %s at FRONTIER_BEST after validation retry "
        "(item=%s)",
        caller, retry_missing, item_id,
    )
    _emit("validation_failed", retry.tier_used or ModelTier.FRONTIER_BEST.value)
    return _as_validation_failed(retry, retry_missing)


def _missing_fields(
    resp: LLMResponse, required_fields: tuple[str, ...],
) -> list[str]:
    struct = resp.structured_output or {}
    return [f for f in required_fields if not struct.get(f)]


def _as_validation_failed(
    resp: LLMResponse, missing: list[str],
) -> LLMResponse:
    """Annotate ``resp`` as a VALIDATION_FAILED error while preserving content."""
    return replace(
        resp,
        error=f"Verdict missing required field(s): {missing}",
        error_kind=ErrorKind.VALIDATION_FAILED,
        hint=(
            "The response parsed against the JSON schema but omitted a "
            "field the caller requires. Consider whether the schema "
            "should mark the field as ``required`` at the backend."
        ),
    )
