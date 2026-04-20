"""Normalized LLM response types for the unified :mod:`work_buddy.llm` runner.

One :class:`LLMResponse` covers every backend — Anthropic, LM Studio
native, and openai-compat — so callers never branch on which tier
actually produced the reply. Backend-specific concepts (LM Studio's
``response_id``, Anthropic's content-block array) live in
``backend_extra`` and should not be read by normal callers.

The :class:`ErrorKind` taxonomy mirrors
:class:`work_buddy.llm.backends._errors.LocalInferenceError`'s ``kind``
strings so the existing LM Studio error interpretation maps straight
through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorKind(str, Enum):
    """Category discriminator for backend failures.

    Mirrors :class:`LocalInferenceError.kind` for LM Studio and extends
    with Anthropic-side categories. Escalation policies check against
    these values to decide whether to try the next tier.
    """

    TIMEOUT = "timeout"
    CONTEXT_EXCEEDED = "context_exceeded"
    EMPTY_CONTENT = "empty_content"
    SCHEMA_VIOLATION = "schema_violation"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    AUTH = "auth"
    RATE_LIMITED = "rate_limited"
    TOOL_EXECUTION = "tool_execution"
    MODEL_NOT_LOADED = "model_not_loaded"
    MODEL_UNSUPPORTED = "model_unsupported"
    BAD_REQUEST = "bad_request"
    MALFORMED_RESPONSE = "malformed_response"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation emitted by the model during a call.

    Attributes:
        name: Capability-level name. For LM Studio MCP dispatch this is
            the concrete capability (e.g. ``"triage_submit"``); for
            Anthropic it's the ``name`` from the ``tool_use`` block.
        arguments: Kwargs the model supplied.
        result: Tool return value, or ``None`` if the backend didn't
            execute the tool (Anthropic single-round, post-dispatch).
        status: ``"ok" | "error" | "pending"`` — ``pending`` means the
            tool was emitted but not executed (rare, multi-turn only).
        artifact_id: Optional pointer into the artifact store when the
            raw tool output was elided from the response.
    """

    name: str
    arguments: dict = field(default_factory=dict)
    result: Any | None = None
    status: str = "ok"
    artifact_id: str | None = None


@dataclass(frozen=True)
class TierAttempt:
    """Audit record for one tier attempt in an escalating call.

    The full sequence lives in :attr:`LLMResponse.tier_attempts` so
    callers can reconstruct why a particular tier ended up answering.
    """

    tier: str                     # ModelTier value; stored as str to avoid cycles
    model: str
    error_kind: ErrorKind | None
    error: str | None
    elapsed_ms: int


@dataclass(frozen=True)
class LLMResponse:
    """Normalized result of an :class:`LLMRunner` call.

    Non-error fields are always safe to read; ``error`` / ``error_kind``
    populate only when the call failed on every attempted tier. Token
    and cost fields reflect the *final* successful attempt; the full
    per-attempt breakdown is in ``tier_attempts``.
    """

    # Output
    content: str = ""
    structured_output: dict | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning: str | None = None
    reasoning_artifact_id: str | None = None

    # Tier / model
    tier_used: str = ""                        # ModelTier value
    tier_attempts: tuple[TierAttempt, ...] = ()
    model: str = ""
    backend: str = ""                          # "anthropic" | "lmstudio_native" | "openai_compat"

    # Accounting
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    cache_key: str | None = None

    # Error
    error: str | None = None
    error_kind: ErrorKind | None = None
    hint: str | None = None

    # Backend-specific escape hatch. Callers should not read from this
    # directly; it exists so the runner can pass through things like
    # LM Studio's ``response_id`` for callers that genuinely need
    # multi-turn state. Treat as opaque.
    backend_extra: dict = field(default_factory=dict)

    def is_error(self) -> bool:
        """True if the call failed on every attempted tier."""
        return self.error is not None or self.error_kind is not None

    def to_legacy_dict(self) -> dict[str, Any]:
        """Flatten to the shape older callers expect.

        During Phase 3 migration, the compat shim in
        :mod:`work_buddy.llm.call` uses this to preserve the legacy
        return shape for callers that haven't updated yet.
        """
        return {
            "content": self.content,
            "parsed": self.structured_output,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached": self.cached,
            "cache_key": self.cache_key,
            "error": self.error,
            "error_kind": self.error_kind.value if self.error_kind else None,
            "hint": self.hint,
            "tool_calls": [
                {
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "result": tc.result,
                    "status": tc.status,
                }
                for tc in self.tool_calls
            ],
        }
