"""Telemetry — the metrics-listener surface for guarded calls.

A guarded call emits typed events; listeners registered on the framework
receive them. Strategies never log directly — observability is wired
through listeners. ``InMemoryMetrics`` is the default listener: a bounded
in-process recorder a dashboard can snapshot, in the same spirit as the
broker's ``SlotMetrics`` ring.

See ``.data/designs/resilience-framework/DESIGN.md`` §7, §10.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from work_buddy.resilience.outcome import OutcomeKind


@dataclass(frozen=True)
class GuardEvent:
    """Base class for resilience telemetry events."""

    operation_key: str
    call_id: str


@dataclass(frozen=True)
class CallCompleted(GuardEvent):
    """One guarded call finished — the pipeline-level signal
    (``guard.call.duration`` in the §10 signal set)."""

    duration_s: float
    outcome: OutcomeKind
    error_type: str | None = None
    parent_call_id: str | None = None


# Strategy-level events (AttemptCompleted, CircuitStateChanged, LoadShed,
# ...) are added in stage B1, alongside the strategies that emit them.


@runtime_checkable
class TelemetryListener(Protocol):
    """Receives guarded-call telemetry events."""

    def on_event(self, event: GuardEvent) -> None: ...


@dataclass
class InMemoryMetrics:
    """Default listener — a bounded in-process recorder.

    Keeps a ring of recent ``CallCompleted`` rows and per-(operation,
    outcome) counters. ``snapshot()`` serializes both for a dashboard or a
    test.
    """

    ring_size: int = 1000
    _events: list[CallCompleted] = field(default_factory=list, repr=False)
    _counts: dict[tuple[str, str], int] = field(
        default_factory=dict, repr=False,
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def on_event(self, event: GuardEvent) -> None:
        if not isinstance(event, CallCompleted):
            return
        with self._lock:
            self._events.append(event)
            if len(self._events) > self.ring_size:
                self._events = self._events[-self.ring_size:]
            key = (event.operation_key, event.outcome.value)
            self._counts[key] = self._counts.get(key, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            events = list(self._events)
            counts = dict(self._counts)
        durations = [e.duration_s for e in events]
        return {
            "call_count": len(events),
            "counts_by_operation_outcome": {
                f"{op}/{outcome}": n
                for (op, outcome), n in sorted(counts.items())
            },
            "duration_s": {
                "min": min(durations) if durations else None,
                "max": max(durations) if durations else None,
                "mean": (
                    sum(durations) / len(durations) if durations else None
                ),
            },
        }

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._counts.clear()


# --- process-global listener registry --------------------------------------

_LISTENERS: list[TelemetryListener] = []
_LISTENERS_LOCK = threading.Lock()


def register_listener(listener: TelemetryListener) -> None:
    """Register a listener to receive every guarded-call event. Idempotent."""
    with _LISTENERS_LOCK:
        if listener not in _LISTENERS:
            _LISTENERS.append(listener)


def get_listeners() -> list[TelemetryListener]:
    with _LISTENERS_LOCK:
        return list(_LISTENERS)


def emit(event: GuardEvent) -> None:
    """Deliver an event to every registered listener.

    Never raises — a misbehaving listener must not break a guarded call.
    """
    for listener in get_listeners():
        try:
            listener.on_event(event)
        except Exception:  # noqa: BLE001 - listener isolation is deliberate
            pass


def _reset_listeners_for_tests() -> None:
    with _LISTENERS_LOCK:
        _LISTENERS.clear()
