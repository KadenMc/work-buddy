"""Resilience-framework adapter for the Obsidian bridge.

Presents an Obsidian bridge call as a resilience *guarded call*: typed
``ObsidianError`` exceptions and the legacy ``bridge_failure`` return-dict
are both mapped onto the outcome taxonomy, ``ObsidianPostWriteUncertain`` is
passed through untouched for the gateway's verify-then-decide path, and the
unified ``guard.*`` telemetry is emitted.

The "participate, don't rewrite" adapter for *ad-hoc* bridge calls that
don't go through the ``@bridge_retry`` decorator. It does NOT add retry —
the framework's one-retry-layer rule means an outer layer (``@bridge_retry``
itself, or ``build_obsidian_pipeline()`` below) owns retry for the call.
``@bridge_retry`` is itself a Retry strategy expressed as a decorator: it
composes ``RetryStrategy`` → ``_BridgeHealthGate`` → call via
``guarded_call_sync``. Decorated calls and adapter calls therefore share
one framework foundation.

Deadline propagation: an already-expired deadline yields ``REJECTED`` at the
seam without running ``fn``. Deeper integration (clamping the bridge's own
HTTP timeout to the remaining budget) is a follow-on tuning.

Import direction: lives under ``work_buddy.obsidian`` and depends on
``work_buddy.resilience`` (the foundation), never the reverse.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, TypeVar

from work_buddy.obsidian.errors import (
    ObsidianError,
    ObsidianNotRunning,
    ObsidianPluginDisabled,
    ObsidianPluginMissing,
    ObsidianPostWriteUncertain,
    ObsidianRefused,
    ObsidianTimeout,
)
from work_buddy.obsidian.retry import is_bridge_failure, is_terminal_bridge_failure
from work_buddy.resilience import (
    Deadline,
    Outcome,
    OutcomeKind,
    ResiliencePipeline,
    ResiliencePipelineBuilder,
    default_classify,
    guarded_call,
)

T = TypeVar("T")

#: Exceptions the bridge raises as control-flow signals, not faults. The
#: gateway's verify-then-decide path needs ``ObsidianPostWriteUncertain`` as
#: a raised exception, so the adapter passes it through ``guarded_call``
#: untouched rather than classifying it into an Outcome (DESIGN §15).
OBSIDIAN_PASSTHROUGH: tuple[type[BaseException], ...] = (
    ObsidianPostWriteUncertain,
)

# Terminal ObsidianError types — retrying will not help; the user must act
# out of band (open Obsidian, install / enable the plugin) or the request is
# structurally rejected (a non-409 4xx). Mapped to TERMINAL_FAILURE.
_TERMINAL_OBSIDIAN_ERRORS: tuple[type[BaseException], ...] = (
    ObsidianNotRunning,
    ObsidianPluginMissing,
    ObsidianPluginDisabled,
    ObsidianRefused,
)


def classify_obsidian_error(exc: BaseException) -> OutcomeKind:
    """Map an Obsidian bridge exception onto the outcome taxonomy.

    Terminal types (Obsidian not running, plugin missing / disabled, 4xx
    refusal) → ``TERMINAL_FAILURE``. ``ObsidianTimeout`` → ``TIMEOUT``. Any
    other ``ObsidianError`` (startup race, editor conflict, 5xx server
    error) → ``TRANSIENT_FAILURE``. Non-Obsidian exceptions fall through to
    the framework default classifier.

    ``ObsidianPostWriteUncertain`` should not reach here — it is a
    passthrough exception (``OBSIDIAN_PASSTHROUGH``), re-raised before
    classification. The defensive ``ObsidianTimeout`` branch would still
    classify it as ``TIMEOUT`` if it ever did.
    """
    if isinstance(exc, _TERMINAL_OBSIDIAN_ERRORS):
        return OutcomeKind.TERMINAL_FAILURE
    if isinstance(exc, ObsidianTimeout):
        return OutcomeKind.TIMEOUT
    if isinstance(exc, ObsidianError):
        return OutcomeKind.TRANSIENT_FAILURE
    return default_classify(exc)


def classify_bridge_result(result: Any) -> OutcomeKind | None:
    """Map a legacy ``bridge_failure`` return-dict onto the taxonomy.

    ``@bridge_retry``-decorated functions return a ``bridge_failure`` dict
    (rather than raising) on retry exhaustion. A terminal bridge failure
    (Obsidian not running, plugin missing / disabled) → ``TERMINAL_FAILURE``;
    any other bridge failure → ``TRANSIENT_FAILURE``. A genuine success
    value → ``None`` (the guarded call keeps it as a success).
    """
    if is_terminal_bridge_failure(result):
        return OutcomeKind.TERMINAL_FAILURE
    if is_bridge_failure(result):
        return OutcomeKind.TRANSIENT_FAILURE
    return None


async def guarded_bridge_call(
    fn: Callable[[], T],
    *,
    operation_key: str,
    deadline: Deadline | None = None,
) -> Outcome:
    """Run an Obsidian bridge call ``fn`` as a resilience guarded call.

    ``fn`` is a *synchronous* callable performing the bridge operation; it
    runs in a worker thread so the bridge's blocking HTTP I/O never stalls
    the event loop. The resilience context — hence the deadline — is
    snapshotted into that thread by ``asyncio.to_thread``.

    Typed ``ObsidianError`` exceptions and ``bridge_failure`` return-dicts
    are mapped onto the taxonomy; ``ObsidianPostWriteUncertain`` propagates
    untouched (re-raised, not returned as an Outcome). Retry is NOT added
    here — an outer layer owns retry (``@bridge_retry`` for decorated
    capabilities, ``build_obsidian_pipeline()`` for explicitly-composed
    pipelines), per the one-retry-layer rule.

    Returns an :class:`Outcome` — except that ``ObsidianPostWriteUncertain``
    propagates as a raise.
    """

    async def _guarded() -> T:
        return await asyncio.to_thread(fn)

    return await guarded_call(
        operation_key,
        _guarded,
        deadline=deadline,
        classify=classify_obsidian_error,
        result_classifier=classify_bridge_result,
        passthrough_exceptions=OBSIDIAN_PASSTHROUGH,
    )


# ---------------------------------------------------------------------------
# The Obsidian resilience pipeline (standalone composition for ad-hoc callers)
# ---------------------------------------------------------------------------


def build_obsidian_pipeline(
    *,
    name: str = "obsidian",
    max_attempts: int = 3,
    retry_base_delay_s: float = 1.0,
    retry_max_delay_s: float = 30.0,
    circuit_failure_threshold: int = 5,
    circuit_reset_timeout_s: float = 30.0,
) -> ResiliencePipeline:
    """Build the resilience pipeline for Obsidian bridge calls.

    Composition (outermost-first): ``Retry`` → ``CircuitBreaker`` → call.

    - ``Retry`` re-attempts ``TIMEOUT`` / ``TRANSIENT_FAILURE`` outcomes.
      Terminal Obsidian errors (plugin disabled / missing, Obsidian not
      running, a 4xx refusal) classify to ``TERMINAL_FAILURE`` and so are
      not retried — the taxonomy encodes ``@bridge_retry``'s terminal
      short-circuit for free.
    - ``CircuitBreaker`` trips after repeated transient failures, so a
      genuinely-down bridge stops being hammered.
    - ``classify_obsidian_error`` / ``classify_bridge_result`` map the typed
      ``ObsidianError`` hierarchy and the legacy ``bridge_failure`` dicts
      onto the outcome taxonomy.
    - ``ObsidianPostWriteUncertain`` is a passthrough exception — it
      propagates to the gateway's verify-then-decide path untouched.

    Standalone alternative to ``@bridge_retry`` for bridge calls that are
    *not* routed through the decorator (ad-hoc gateway code, future
    explicitly-composed call sites). Adds a circuit breaker on top of the
    same Retry primitive that ``@bridge_retry`` uses, so a genuinely-down
    bridge stops being hammered after repeated transient failures.

    ``@bridge_retry`` itself is a thin shim over a
    ``RetryStrategy`` → ``_BridgeHealthGate`` → call chain — a
    decorator-shaped use of the same framework primitives this pipeline
    composes. The two coexist; the one-retry-layer rule means a given call
    goes through exactly one of them.

    The retry-cadence defaults are sane transient-failure values; the exact
    schedule is a tuning decision for the call site, balanced against the
    propagating deadline.
    """
    return (
        ResiliencePipelineBuilder(name)
        .retry(
            max_attempts=max_attempts,
            base_delay_s=retry_base_delay_s,
            max_delay_s=retry_max_delay_s,
        )
        .circuit_breaker(
            name=f"{name}-circuit",
            failure_threshold=circuit_failure_threshold,
            reset_timeout_s=circuit_reset_timeout_s,
        )
        .classify(classify_obsidian_error)
        .result_classifier(classify_bridge_result)
        .passthrough(*OBSIDIAN_PASSTHROUGH)
        .build()
    )
