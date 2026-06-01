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

from work_buddy.resilience import InMemoryMetrics, register_listener
from work_buddy.resilience.telemetry import (
    CallCompleted,
    CircuitStateChanged,
    GuardEvent,
    LoadShed,
)

logger = logging.getLogger("work_buddy.mcp_server.dispatch")

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
