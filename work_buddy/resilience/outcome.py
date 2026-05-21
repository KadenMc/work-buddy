"""Outcome — the unified result type of a guarded call.

Every strategy in the resilience framework returns an ``Outcome``; none
raises to signal a policy decision. The ``kind`` is the coarse, uniform
classification — the outcome taxonomy — that the framework, dashboards, and
the circuit breaker key on. It does not replace the rich typed exceptions
the underlying systems raise; it sits above them.

See ``.data/designs/resilience-framework/DESIGN.md`` §9.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Generic, Mapping, TypeVar

T = TypeVar("T")


class OutcomeKind(enum.Enum):
    """The outcome taxonomy.

    The classification carried on every ``Outcome``. The granularity
    (transient vs terminal vs rejected) exists so a circuit breaker can
    decide which outcomes count toward tripping without re-inspecting the
    underlying exception.
    """

    SUCCESS = "success"
    TIMEOUT = "timeout"
    REJECTED = "rejected"
    TRANSIENT_FAILURE = "transient_failure"
    TERMINAL_FAILURE = "terminal_failure"
    PARTIAL = "partial"

    @property
    def is_success(self) -> bool:
        return self is OutcomeKind.SUCCESS

    @property
    def is_failure(self) -> bool:
        """True for the three failure kinds.

        ``REJECTED`` is not a failure — the call was shed and never ran.
        ``PARTIAL`` is a qualified success, not a failure.
        """
        return self in (
            OutcomeKind.TIMEOUT,
            OutcomeKind.TRANSIENT_FAILURE,
            OutcomeKind.TERMINAL_FAILURE,
        )

    @property
    def is_retryable(self) -> bool:
        """True for outcomes a Retry strategy may re-attempt.

        Terminal failures will not improve on retry; rejected and partial
        outcomes are not retry's concern.
        """
        return self in (OutcomeKind.TIMEOUT, OutcomeKind.TRANSIENT_FAILURE)

    @property
    def counts_toward_circuit_trip(self) -> bool:
        """True for outcomes a circuit breaker should count as evidence
        the dependency is unhealthy.

        Rejected calls never ran; terminal failures are handled by
        short-circuit, not by the breaker.
        """
        return self in (OutcomeKind.TIMEOUT, OutcomeKind.TRANSIENT_FAILURE)


class OutcomeError(Exception):
    """Raised by ``Outcome.unwrap()`` for a failed outcome that carries no
    captured exception of its own."""

    def __init__(self, kind: OutcomeKind, detail: str = "") -> None:
        super().__init__(detail or f"guarded call failed: {kind.value}")
        self.kind = kind


@dataclass(frozen=True)
class Outcome(Generic[T]):
    """The result of a guarded call: a value XOR an error, plus its kind.

    Immutable. Construct via :meth:`success` / :meth:`failure`, not the bare
    constructor.
    """

    kind: OutcomeKind
    value: T | None = None
    error: BaseException | None = None
    detail: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def success(
        cls, value: T, *, detail: str = "", **metadata: Any
    ) -> "Outcome[T]":
        return cls(
            OutcomeKind.SUCCESS, value=value, detail=detail,
            metadata=dict(metadata),
        )

    @classmethod
    def failure(
        cls,
        kind: OutcomeKind,
        *,
        error: BaseException | None = None,
        detail: str = "",
        **metadata: Any,
    ) -> "Outcome[T]":
        if kind is OutcomeKind.SUCCESS:
            raise ValueError("Outcome.failure() requires a non-SUCCESS kind")
        return cls(
            kind, error=error, detail=detail, metadata=dict(metadata),
        )

    @property
    def is_success(self) -> bool:
        return self.kind.is_success

    def unwrap(self) -> T:
        """Return the value on success; re-raise the error otherwise.

        If a failed outcome carries no captured exception, raises
        :class:`OutcomeError`.
        """
        if self.kind.is_success:
            return self.value  # type: ignore[return-value]
        if self.error is not None:
            raise self.error
        raise OutcomeError(self.kind, self.detail)
