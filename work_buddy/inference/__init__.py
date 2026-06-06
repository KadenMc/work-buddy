"""Local inference orchestration — the broker layer.

Sits between callers (``work_buddy.llm.backends.*``,
``work_buddy.embedding.providers.*``) and the actual HTTP calls to
LM Studio / OpenAI-compat servers. Its job is to decide **when** a
given request runs, not **how**: it gates on a per-profile slot
semaphore with priority-aware admission, and it emits per-call
metrics so downstream work (dashboard, triage) can see what's
happening inside the otherwise-opaque LM Studio queue.

See ``work_buddy.inference.broker`` for the public API.
"""

from work_buddy.inference.broker import (
    InferenceTimeout,
    LocalInferenceBroker,
    Priority,
    ProfileConfig,
    QueueFull,
    QueueWaitTimeout,
    SlotMetrics,
    get_broker,
    parse_priority,
)

__all__ = [
    "InferenceTimeout",
    "LocalInferenceBroker",
    "Priority",
    "ProfileConfig",
    "QueueFull",
    "QueueWaitTimeout",
    "SlotMetrics",
    "get_broker",
    "parse_priority",
]
