"""Resilience wiring for the MCP gateway's capability dispatch.

Every ``wb_run`` capability dispatch runs through the resilience framework's
``guarded_call`` so the gateway emits dispatch-timing telemetry under the
``wb_run:<capability>`` operation key. Listeners registered here record those
events (an in-process metrics ring) and log one grep-able line per dispatch.

Kept lightweight: only the resilience framework (stdlib-only) is imported at
module load, so this never pulls a heavy dependency onto the gateway boot path.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping

from work_buddy.resilience import (
    Deadline,
    InMemoryMetrics,
    TimeoutStrategy,
    register_listener,
)
from work_buddy.resilience.telemetry import (
    CallCompleted,
    CircuitStateChanged,
    GuardEvent,
    LoadShed,
)

logger = logging.getLogger("work_buddy.mcp_server.dispatch")

# Default wall-time budget for a capability dispatch that declares none and is
# not self-managing. Local / leaf operations complete well within this.
DEFAULT_DISPATCH_TIMEOUT_S: float = 30.0

# Tool id whose dependent capabilities self-retry (via @bridge_retry) and own
# their own time budget — the gateway does not impose a timeout on them.
_OBSIDIAN_TOOL_ID = "obsidian"

# In-process recorder of guarded-call telemetry. A status surface or a live
# verification script can snapshot it via :func:`get_dispatch_metrics`.
_METRICS = InMemoryMetrics()

_LISTENERS_READY = False


class _DispatchLogListener:
    """Logs guarded-call telemetry so every dispatch leaves a grep-able line."""

    def on_event(self, event: GuardEvent) -> None:
        if isinstance(event, CallCompleted):
            logger.info(
                "guard.call op=%s outcome=%s dur=%.3fs%s",
                event.operation_key,
                event.outcome.value,
                event.duration_s,
                f" error={event.error_type}" if event.error_type else "",
            )
        elif isinstance(event, CircuitStateChanged):
            logger.warning(
                "guard.circuit name=%s %s->%s op=%s",
                event.name, event.from_state, event.to_state,
                event.operation_key,
            )
        elif isinstance(event, LoadShed):
            logger.warning(
                "guard.shed name=%s reason=%s op=%s",
                event.name, event.reason, event.operation_key,
            )


def ensure_listeners_registered() -> None:
    """Register the gateway's telemetry listeners once (idempotent)."""
    global _LISTENERS_READY
    if _LISTENERS_READY:
        return
    register_listener(_METRICS)
    register_listener(_DispatchLogListener())
    _LISTENERS_READY = True


def get_dispatch_metrics() -> InMemoryMetrics:
    """The in-process recorder of guarded-call telemetry (status / tests)."""
    return _METRICS


# ---------------------------------------------------------------------------
# Timeout budget — operation-derived, param-aware
# ---------------------------------------------------------------------------


def _domain_default(entry: Any) -> float:
    """The budget for a capability that declares none, by operation type."""
    requires = getattr(entry, "requires", None) or []
    if _OBSIDIAN_TOOL_ID in requires:
        return math.inf  # bridge work self-retries; gateway does not time it
    return DEFAULT_DISPATCH_TIMEOUT_S


def _coerce_budget(value: Any, *, fallback: float) -> float:
    """Coerce a declared/derived budget to seconds (``inf`` = unbounded).

    ``None`` → unbounded; a non-positive or non-numeric value → ``fallback``
    (a non-positive timeout is meaningless and ``TimeoutStrategy`` rejects it).
    """
    if value is None:
        return math.inf
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return fallback
    if seconds <= 0 or math.isnan(seconds):
        return math.inf
    return seconds


def resolve_timeout_budget(entry: Any, params: Mapping[str, Any]) -> float:
    """Resolve the wall-time budget (seconds; ``inf`` = no gateway timeout).

    Operation-derived, most-specific-wins: a ``timeout_seconds`` policy
    callable derives from the actual params; a scalar is a fixed ceiling;
    ``None`` (unset) falls to the domain default. Never raises — a policy that
    raises falls back to the domain default.
    """
    declared = getattr(entry, "timeout_seconds", None)
    domain_default = _domain_default(entry)
    if callable(declared):
        try:
            derived = declared(dict(params))
        except Exception:  # noqa: BLE001 - a bad policy must never break dispatch
            logger.warning(
                "timeout policy for %r raised; using domain default",
                getattr(entry, "name", "?"), exc_info=True,
            )
            return domain_default
        return _coerce_budget(derived, fallback=domain_default)
    if declared is not None:
        return _coerce_budget(declared, fallback=domain_default)
    return domain_default


def build_dispatch_deadline(budget: float) -> Deadline:
    """The propagating deadline for a dispatch (``inf`` → never)."""
    return Deadline.never() if budget == math.inf else Deadline.after(budget)


def build_dispatch_strategies(entry: Any, budget: float) -> list:
    """The resilience strategy chain for one capability dispatch.

    A bounded budget adds a ``TimeoutStrategy``; an unbounded one adds none.
    No retry strategy is added here — the one-retry-layer rule reserves retry
    for the inner chain (``@bridge_retry``-decorated capabilities).
    """
    strategies: list = []
    if budget != math.inf:
        strategies.append(TimeoutStrategy(budget))
    return strategies
