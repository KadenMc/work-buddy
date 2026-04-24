"""Shared verdict-call helper with post-response validation escalation.

Both :mod:`work_buddy.triage.capabilities.inline_triage_scan` and
:mod:`work_buddy.triage.capabilities.journal_triage_scan` share an
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
    )
    if resp.is_error():
        return resp

    missing = _missing_fields(resp, required_fields)
    if not missing:
        return resp

    used = resp.tier_used or tier.value
    if used == ModelTier.FRONTIER_BEST.value:
        # Already at top tier — no further escalation possible.
        logger.warning(
            "%s: verdict missing %s at final tier=%s (item=%s)",
            caller, missing, used, item_id,
        )
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
    )
    if retry.is_error():
        return retry

    retry_missing = _missing_fields(retry, required_fields)
    if not retry_missing:
        return retry

    logger.warning(
        "%s: verdict missing %s at FRONTIER_BEST after validation retry "
        "(item=%s)",
        caller, retry_missing, item_id,
    )
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
