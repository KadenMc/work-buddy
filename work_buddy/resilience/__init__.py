"""work-buddy resilience framework.

A unified foundation for fault mitigation across guarded calls — the
propagating Deadline, the outcome taxonomy, the execution seam, the
telemetry surface, the composable strategy library, and the pipeline /
registry that assembles them.

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
from work_buddy.resilience.pipeline import (
    ResiliencePipeline,
    ResiliencePipelineBuilder,
    ResiliencePipelineRegistry,
    get_pipeline_registry,
)
from work_buddy.resilience.seam import (
    Classifier,
    GuardedFn,
    ResultClassifier,
    ResilienceStrategy,
    default_classify,
    guarded_call,
    guarded_call_sync,
)
from work_buddy.resilience.strategies import (
    PRIORITY,
    BulkheadStrategy,
    CircuitBreakerStrategy,
    CircuitState,
    FallbackStrategy,
    Priority,
    PriorityBulkheadStrategy,
    RateLimiterStrategy,
    RetryStrategy,
    TimeoutStrategy,
)
from work_buddy.resilience.telemetry import (
    CallCompleted,
    CircuitStateChanged,
    GuardEvent,
    InMemoryMetrics,
    LoadShed,
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
    "ResultClassifier",
    "ResilienceStrategy",
    "default_classify",
    "guarded_call",
    "guarded_call_sync",
    # strategies
    "PRIORITY",
    "BulkheadStrategy",
    "CircuitBreakerStrategy",
    "CircuitState",
    "FallbackStrategy",
    "Priority",
    "PriorityBulkheadStrategy",
    "RateLimiterStrategy",
    "RetryStrategy",
    "TimeoutStrategy",
    # pipeline
    "ResiliencePipeline",
    "ResiliencePipelineBuilder",
    "ResiliencePipelineRegistry",
    "get_pipeline_registry",
    # telemetry
    "CallCompleted",
    "CircuitStateChanged",
    "GuardEvent",
    "InMemoryMetrics",
    "LoadShed",
    "TelemetryListener",
    "emit",
    "get_listeners",
    "register_listener",
]
