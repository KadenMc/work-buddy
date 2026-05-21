"""work-buddy resilience framework.

A unified foundation for fault mitigation across guarded calls — the
propagating Deadline, the outcome taxonomy, the execution seam, and the
telemetry surface. Strategies (Timeout, Retry, CircuitBreaker, Bulkhead,
RateLimiter, Fallback) arrive in a later stage; this package currently
provides the spine they compose onto.

Design: ``.data/designs/resilience-framework/DESIGN.md``.
"""

from work_buddy.resilience.context import (
    ResilienceContext,
    TypedKey,
    current_context,
    use_context,
)
from work_buddy.resilience.deadline import Deadline
from work_buddy.resilience.outcome import Outcome, OutcomeError, OutcomeKind
from work_buddy.resilience.seam import (
    Classifier,
    GuardedFn,
    Strategy,
    default_classify,
    guarded_call,
    guarded_call_sync,
)
from work_buddy.resilience.telemetry import (
    CallCompleted,
    GuardEvent,
    InMemoryMetrics,
    TelemetryListener,
    emit,
    get_listeners,
    register_listener,
)

__all__ = [
    # context
    "ResilienceContext",
    "TypedKey",
    "current_context",
    "use_context",
    # deadline
    "Deadline",
    # outcome / taxonomy
    "Outcome",
    "OutcomeError",
    "OutcomeKind",
    # seam
    "Classifier",
    "GuardedFn",
    "Strategy",
    "default_classify",
    "guarded_call",
    "guarded_call_sync",
    # telemetry
    "CallCompleted",
    "GuardEvent",
    "InMemoryMetrics",
    "TelemetryListener",
    "emit",
    "get_listeners",
    "register_listener",
]
