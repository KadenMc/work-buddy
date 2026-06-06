"""LocalInferenceBroker — admission control for LM Studio / local LLM calls.

Motivation
----------
LM Studio has an internal queue and per-engine concurrency knobs
(``Max Concurrent Predictions``, default 4). Its public API does NOT
expose current slot occupancy, so a caller can burn its timeout budget
sitting inside LM Studio's hidden queue while work-buddy thinks
"nothing's happening." Worse: a background bulk encode can starve an
interactive dashboard search because both hit the same server and the
server has no notion of *our* priorities.

This broker makes work-buddy (not LM Studio) the scheduler of record
for local inference:

* **Per-profile active-slot limits** configured explicitly because we
  can't discover LM Studio's real capacity. Default 1 per profile —
  conservative but predictable.
* **Priority classes.** ``INTERACTIVE`` (dashboard search, UI-driven
  requests) admits ahead of ``WORKFLOW`` (agent-initiated work),
  which admits ahead of ``BACKGROUND`` (cron jobs like
  ir-index-rebuild). Within a class, FIFO.
* **Split timeouts.** ``queue_wait_s`` = how long the caller is
  willing to wait for a slot; ``inference_s`` = how long the
  downstream call itself may take. Two distinct failure modes with
  distinct error kinds so operators can tell "queued too long" apart
  from "model is slow."
* **Per-call metrics.** Queued-at / admitted-at / finished-at
  timestamps + service-time / queue-wait latency splits, kept in a
  bounded ring buffer. The sidecar dashboard can snapshot these.

Scope
-----
Wired into every local-inference call site:

* ``work_buddy.embedding.providers.lmstudio`` — bulk document encode
  (profile prefix ``lmstudio:``). This is the call site that originally
  motivated the broker: a bulk encode holding the embedding service's
  thread + Python locks while an interactive search tried to run.
* ``work_buddy.llm.backends.openai_compat`` (profile prefix
  ``openai_compat:``) and ``work_buddy.llm.backends.lmstudio_native``
  (profile prefix ``lmstudio_native:``) — every local LLM completion.

The per-call-site profile prefix keeps slot limits independent even
when all three point at the same LM Studio instance. Each caller
declares a :class:`Priority` per call: the LLM path threads it from
``LLMRunner.call(priority=...)`` down through ``run_task`` to the
backend slot; the embedding path from ``encode(priority=...)``.
Frontier/Anthropic calls are NOT brokered — Anthropic is a cloud
service with its own rate limiting.

The broker does NOT enforce ``inference_s`` directly — it passes
the value along so the caller's ``httpx.Client(timeout=...)`` does
the HTTP-layer enforcement. The budget is recorded for observability;
a watchdog that force-cancels stuck calls is open work.

Usage
-----
::

    from work_buddy.inference import get_broker, Priority

    broker = get_broker()
    with broker.slot(
        profile="lmstudio_local:text-embedding-snowflake-arctic-embed-m-v1.5",
        priority=Priority.BACKGROUND,
        queue_wait_s=15.0,
        inference_s=60.0,
    ) as ticket:
        ticket.mark_started_http()
        response = httpx_post(...)
    # Metrics emitted on context-manager exit.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API — priorities, errors, config
# ---------------------------------------------------------------------------


class Priority(enum.IntEnum):
    """Priority classes for local-inference requests.

    Lower numeric value = higher priority. Admission honors priority
    ordering across classes and FIFO within a class.
    """

    INTERACTIVE = 0
    """UI-driven / user-is-waiting. Must not sit behind background work."""

    WORKFLOW = 1
    """Agent-initiated work tied to a user request but not UI-facing."""

    BACKGROUND = 2
    """Cron jobs, bulk index rebuilds. Yields to everything else."""


def parse_priority(value: str | Priority | None) -> Priority | None:
    """Coerce a string / enum / ``None`` into a :class:`Priority`.

    Accepts a :class:`Priority` (returned as-is), ``None`` (returned as
    ``None`` so the caller falls through to its own default), or a
    case-insensitive name string (``"interactive"`` / ``"workflow"`` /
    ``"background"``). Exists for the MCP boundary, where capability
    params arrive as JSON strings and must map onto the enum before
    reaching the broker.

    Raises:
        ValueError: the string does not name a known priority.
    """
    if value is None or isinstance(value, Priority):
        return value
    try:
        return Priority[str(value).strip().upper()]
    except KeyError:
        valid = ", ".join(p.name.lower() for p in Priority)
        raise ValueError(
            f"Unknown priority {value!r}; expected one of: {valid}."
        )


class BrokerError(Exception):
    """Base class for broker-raised failures.

    Attributes:
        kind: Short classifier matching
            ``queue_full``/``queue_wait_timeout``/``inference_timeout``.
            Callers can pattern-match on this without stringifying.
        extra: Context dict for structured logging.
    """

    def __init__(self, message: str, *, kind: str, **extra: Any) -> None:
        super().__init__(message)
        self.kind = kind
        self.extra = extra


class QueueFull(BrokerError):
    """The queue at this priority for this profile is at capacity.

    Raised synchronously from ``slot()`` when ``max_queued`` is
    exceeded. The caller should back off rather than wait — the queue
    isn't draining fast enough and piling more on will make it worse.
    """


class QueueWaitTimeout(BrokerError):
    """The request waited for a slot longer than ``queue_wait_s`` and was
    evicted without admitting. Distinct from ``InferenceTimeout``:
    this means "we never even tried to call LM Studio."
    """


class InferenceTimeout(BrokerError):
    """The request got a slot but the downstream call exceeded
    ``inference_s``. The broker does not enforce this directly —
    callers use their own HTTP client timeout. This exception exists
    as a vocabulary for the caller to raise + metrics to record.
    """


@dataclass
class ProfileConfig:
    """Per-profile slot configuration.

    A "profile" is a logical endpoint+model pair. Examples:

    * ``lmstudio_local:text-embedding-snowflake-arctic-embed-m-v1.5``
      (the embedding offload target)
    * ``lmstudio_local:qwen/qwen2.5-coder-14b`` (an LLM profile)

    Conservative defaults: 1 active slot because LM Studio's real
    capacity is config-dependent and not discoverable from the API.
    Users can bump per-profile in ``config.yaml`` when they've
    measured the actual safe concurrency.
    """

    name: str
    max_concurrent: int = 1
    """Max in-flight calls for this profile. Default 1 is the
    conservative-stable choice: matches LM Studio's serial mode for
    models that don't support continuous batching."""

    max_queued: int = 32
    """Max waiting tickets PER PRIORITY class. Exceeding this raises
    ``QueueFull`` synchronously so backpressure propagates to the
    caller instead of silently accumulating."""

    default_queue_wait_s: float = 20.0
    """Default ``queue_wait_s`` when the caller doesn't specify one.
    20s matches the common 30s end-to-end caller timeout minus some
    headroom for the actual inference call."""

    default_inference_s: float = 120.0
    """Default ``inference_s`` for metrics / caller hint. Not enforced
    by the broker directly — callers apply their own HTTP timeout."""


@dataclass
class SlotMetrics:
    """One row per broker-dispatched call, emitted on slot exit.

    Stored in the broker's bounded ring buffer; snapshot via
    ``broker.snapshot_metrics()`` for dashboards.
    """

    id: str
    profile: str
    priority: Priority
    queued_at: float
    admitted_at: float | None = None
    started_http_at: float | None = None
    first_token_at: float | None = None
    finished_at: float | None = None
    status: str = "queued"  # queued|running|ok|queue_full|queue_wait_timeout|inference_timeout|error
    error_kind: str | None = None
    error_detail: str | None = None

    def queue_wait_ms(self) -> float | None:
        if self.admitted_at is None:
            return None
        return (self.admitted_at - self.queued_at) * 1000.0

    def service_time_ms(self) -> float | None:
        if self.admitted_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.admitted_at) * 1000.0

    def total_latency_ms(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.queued_at) * 1000.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile": self.profile,
            "priority": self.priority.name,
            "queued_at": self.queued_at,
            "admitted_at": self.admitted_at,
            "started_http_at": self.started_http_at,
            "first_token_at": self.first_token_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "error_kind": self.error_kind,
            "error_detail": self.error_detail,
            "queue_wait_ms": self.queue_wait_ms(),
            "service_time_ms": self.service_time_ms(),
            "total_latency_ms": self.total_latency_ms(),
        }


@dataclass
class Ticket:
    """Handed to the caller inside ``slot()``. Lets the caller annotate
    the metrics record with HTTP-start / first-token timestamps."""

    id: str
    profile: str
    priority: Priority
    queue_wait_s: float
    inference_s: float
    _metrics: SlotMetrics | None = field(default=None, repr=False)

    def mark_started_http(self) -> None:
        if self._metrics is not None:
            self._metrics.started_http_at = time.monotonic()

    def mark_first_token(self) -> None:
        if self._metrics is not None:
            self._metrics.first_token_at = time.monotonic()


# ---------------------------------------------------------------------------
# Internal per-profile state
# ---------------------------------------------------------------------------


class _ProfileState:
    """Slot semaphore + priority-aware admission for one profile.

    Uses a single Condition and a counter of waiting-tickets-by-priority
    to enforce "no lower-priority ticket admits while a higher-priority
    ticket is waiting." Waiters use predicate-driven ``Condition.wait``
    with a deadline — no thread can hang indefinitely.
    """

    def __init__(self, cfg: ProfileConfig) -> None:
        self.cfg = cfg
        self._cv = threading.Condition()
        self._in_flight: int = 0
        self._waiting: dict[Priority, int] = {p: 0 for p in Priority}

    def reconfigure(self, cfg: ProfileConfig) -> None:
        with self._cv:
            self.cfg = cfg
            # Capacity may have increased — wake everyone to re-check.
            self._cv.notify_all()

    def _can_admit(self, priority: Priority) -> bool:
        """True iff a ticket at this priority may admit right now.

        Must be called with ``self._cv`` held.
        """
        if self._in_flight >= self.cfg.max_concurrent:
            return False
        # Respect priority: if any higher-priority ticket is waiting,
        # don't admit lower-priority tickets ahead of it.
        for p in Priority:
            if p.value < priority.value and self._waiting[p] > 0:
                return False
        return True

    def admit(self, ticket: Ticket, metrics: SlotMetrics) -> bool:
        """Attempt to acquire a slot within the ticket's queue-wait budget.

        Returns ``True`` on successful admission (in-flight incremented;
        caller must call :meth:`release` eventually).
        Returns ``False`` on queue-wait timeout.
        Raises :class:`QueueFull` if the per-priority queue is at capacity.
        """
        deadline = time.monotonic() + ticket.queue_wait_s

        with self._cv:
            if self._waiting[ticket.priority] >= self.cfg.max_queued:
                raise QueueFull(
                    f"Queue at {ticket.priority.name} priority on "
                    f"{self.cfg.name!r} is at capacity "
                    f"({self.cfg.max_queued}).",
                    kind="queue_full",
                    profile=self.cfg.name,
                    priority=ticket.priority.name,
                    max_queued=self.cfg.max_queued,
                )

            self._waiting[ticket.priority] += 1
            try:
                while not self._can_admit(ticket.priority):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    # Wait on the condition; release (notify_all) will
                    # wake us to re-check the predicate.
                    self._cv.wait(timeout=remaining)
                # Admit.
                self._in_flight += 1
                metrics.admitted_at = time.monotonic()
                return True
            finally:
                self._waiting[ticket.priority] -= 1
                # Notify others — our waiting-count decrement may let
                # a lower-priority ticket admit, OR (if we succeeded)
                # our in_flight bump affects their predicate.
                self._cv.notify_all()

    def release(self, ticket: Ticket) -> None:
        with self._cv:
            self._in_flight -= 1
            self._cv.notify_all()

    def status(self) -> dict[str, Any]:
        with self._cv:
            return {
                "name": self.cfg.name,
                "max_concurrent": self.cfg.max_concurrent,
                "max_queued": self.cfg.max_queued,
                "in_flight": self._in_flight,
                "waiting": {
                    p.name: self._waiting[p] for p in Priority
                },
            }


# ---------------------------------------------------------------------------
# The broker itself
# ---------------------------------------------------------------------------


class LocalInferenceBroker:
    """Process-global scheduler for local inference calls.

    One instance per Python process; use :func:`get_broker` to retrieve
    it. Register profiles via :meth:`configure_profile` (or let the
    broker auto-register with default config on first use).
    """

    _METRICS_RING_SIZE = 1000

    def __init__(self) -> None:
        self._profiles: dict[str, _ProfileState] = {}
        self._lock = threading.Lock()
        self._metrics: list[SlotMetrics] = []
        self._metrics_lock = threading.Lock()

    def configure_profile(self, cfg: ProfileConfig) -> None:
        """Register or reconfigure a profile. Idempotent."""
        with self._lock:
            state = self._profiles.get(cfg.name)
            if state is None:
                self._profiles[cfg.name] = _ProfileState(cfg)
            else:
                state.reconfigure(cfg)

    def _ensure_profile(self, name: str) -> _ProfileState:
        """Return the profile state, auto-registering with defaults if
        not yet configured. Keeps call-site code clean.
        """
        with self._lock:
            state = self._profiles.get(name)
            if state is None:
                state = _ProfileState(ProfileConfig(name=name))
                self._profiles[name] = state
                logger.debug(
                    "Broker auto-registered profile %r with defaults", name,
                )
            return state

    @contextmanager
    def slot(
        self,
        *,
        profile: str,
        priority: Priority = Priority.WORKFLOW,
        queue_wait_s: float | None = None,
        inference_s: float | None = None,
    ) -> Iterator[Ticket]:
        """Acquire a slot for one local-inference call.

        Blocks until admitted or ``queue_wait_s`` elapses. On admission,
        yields a :class:`Ticket` the caller can annotate with
        ``mark_started_http()`` / ``mark_first_token()``. On exit (normal
        or exceptional), releases the slot and records metrics.

        Raises:
            QueueFull: per-priority queue is at capacity.
            QueueWaitTimeout: ``queue_wait_s`` elapsed without admission.
            Any exception from the wrapped block — propagated after
            metrics are recorded and the slot released.
        """
        state = self._ensure_profile(profile)
        wait_s = (
            queue_wait_s
            if queue_wait_s is not None
            else state.cfg.default_queue_wait_s
        )
        infer_s = (
            inference_s
            if inference_s is not None
            else state.cfg.default_inference_s
        )

        metrics = SlotMetrics(
            id=uuid.uuid4().hex[:12],
            profile=profile,
            priority=priority,
            queued_at=time.monotonic(),
        )
        ticket = Ticket(
            id=metrics.id,
            profile=profile,
            priority=priority,
            queue_wait_s=wait_s,
            inference_s=infer_s,
            _metrics=metrics,
        )
        self._record(metrics)

        try:
            admitted = state.admit(ticket, metrics)
        except QueueFull:
            metrics.status = "queue_full"
            metrics.finished_at = time.monotonic()
            raise

        if not admitted:
            metrics.status = "queue_wait_timeout"
            metrics.finished_at = time.monotonic()
            raise QueueWaitTimeout(
                f"Waited {wait_s:.1f}s for a slot on profile {profile!r} "
                f"at priority {priority.name}; giving up.",
                kind="queue_wait_timeout",
                profile=profile,
                priority=priority.name,
                queue_wait_s=wait_s,
            )

        try:
            metrics.status = "running"
            yield ticket
            metrics.status = "ok"
        except BrokerError as exc:
            metrics.status = exc.kind
            metrics.error_kind = exc.kind
            metrics.error_detail = str(exc)[:200]
            raise
        except Exception as exc:
            metrics.status = "error"
            metrics.error_kind = type(exc).__name__
            metrics.error_detail = str(exc)[:200]
            raise
        finally:
            metrics.finished_at = time.monotonic()
            state.release(ticket)

    def _record(self, metrics: SlotMetrics) -> None:
        """Append metrics to the ring buffer, dropping oldest if full."""
        with self._metrics_lock:
            self._metrics.append(metrics)
            if len(self._metrics) > self._METRICS_RING_SIZE:
                # Drop from the front. Slice copy keeps it cheap enough
                # for our expected volume (a few thousand/day max).
                self._metrics = self._metrics[-self._METRICS_RING_SIZE:]

    def snapshot_metrics(
        self, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return serialized metrics for the last N calls (default: all).

        Copies the list to avoid leaking the ring-buffer reference.
        """
        with self._metrics_lock:
            rows = self._metrics[-limit:] if limit else list(self._metrics)
        return [m.to_dict() for m in rows]

    def profile_status(self) -> dict[str, dict[str, Any]]:
        """Per-profile occupancy snapshot for dashboards / diagnostics."""
        with self._lock:
            profiles = list(self._profiles.items())
        return {name: state.status() for name, state in profiles}


# ---------------------------------------------------------------------------
# Process-wide singleton + config loader
# ---------------------------------------------------------------------------


_BROKER: LocalInferenceBroker | None = None
_BROKER_LOCK = threading.Lock()


def get_broker() -> LocalInferenceBroker:
    """Return the process-global broker singleton.

    Thread-safe and idempotent. Auto-loads profile config from
    ``inference.profiles.<name>`` on first call; missing config is
    fine — profiles auto-register with defaults on first use.
    """
    global _BROKER
    with _BROKER_LOCK:
        if _BROKER is None:
            broker = LocalInferenceBroker()
            try:
                _configure_from_config(broker)
            except Exception as exc:
                logger.debug(
                    "Broker config load deferred (%s: %s)",
                    type(exc).__name__, exc,
                )
            _BROKER = broker
        return _BROKER


def _reset_broker_for_tests() -> None:
    """Drop the singleton so tests can exercise fresh configurations."""
    global _BROKER
    with _BROKER_LOCK:
        _BROKER = None


def _configure_from_config(broker: LocalInferenceBroker) -> None:
    """Register profiles declared under ``inference.profiles`` in config.

    Schema::

        inference:
          profiles:
            lmstudio_local:text-embedding-snowflake-arctic-embed-m-v1.5:
              max_concurrent: 1
              max_queued: 16
              default_queue_wait_s: 15
              default_inference_s: 60
    """
    from work_buddy.config import load_config

    cfg = load_config()
    profiles = cfg.get("inference", {}).get("profiles", {}) or {}
    for name, pcfg in profiles.items():
        if not isinstance(pcfg, dict):
            continue
        broker.configure_profile(ProfileConfig(
            name=name,
            max_concurrent=int(pcfg.get("max_concurrent", 1)),
            max_queued=int(pcfg.get("max_queued", 32)),
            default_queue_wait_s=float(
                pcfg.get("default_queue_wait_s", 20.0)
            ),
            default_inference_s=float(
                pcfg.get("default_inference_s", 120.0)
            ),
        ))
