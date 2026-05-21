"""Deadline — an absolute, propagating stop-time.

A ``Deadline`` is an absolute point on the ``time.monotonic()`` clock, not a
relative duration. Absolute deadlines compose cleanly through a nested call
tree: each layer clamps its own timeout to what remains, with no per-layer
subtraction and no clock-skew.

See ``.data/designs/resilience-framework/DESIGN.md`` §8.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

_INF = float("inf")


@dataclass(frozen=True)
class Deadline:
    """An absolute stop-time on the ``time.monotonic()`` clock."""

    at: float
    """Absolute monotonic timestamp. ``float('inf')`` means unbounded."""

    @classmethod
    def after(cls, seconds: float) -> "Deadline":
        """A deadline ``seconds`` from now."""
        return cls(at=time.monotonic() + seconds)

    @classmethod
    def never(cls) -> "Deadline":
        """An unbounded deadline — never expires."""
        return cls(at=_INF)

    def remaining(self) -> float:
        """Seconds left before the deadline.

        Negative once passed; ``inf`` for an unbounded deadline.
        """
        if self.at == _INF:
            return _INF
        return self.at - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def clamp(self, local_timeout: float | None) -> float:
        """The effective timeout for a call under this deadline:
        ``min(local_timeout, remaining)``, floored at 0.

        With ``local_timeout=None`` returns the remaining budget. This is
        what an adapter calls to bound an inner I/O timeout (the broker's
        ``inference_s``, the bridge's HTTP timeout) so a nested call can
        never outlive the deadline its caller is honoring.
        """
        rem = self.remaining()
        effective = rem if local_timeout is None else min(local_timeout, rem)
        if effective == _INF:
            return _INF
        return max(effective, 0.0)

    def derive_attempt(self, per_attempt_s: float) -> "Deadline":
        """A sub-deadline for one attempt: ``min(self, now + per_attempt_s)``.

        A Retry strategy derives one of these per attempt so each attempt
        gets its own fresh clock — but never one that extends past the
        parent deadline.
        """
        return Deadline(at=min(self.at, time.monotonic() + per_attempt_s))
