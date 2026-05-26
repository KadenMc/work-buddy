"""Bridge-aware retry for Obsidian-dependent operations.

Two mechanisms:

1. ``@bridge_retry`` decorator — apply to functions that depend on the
   Obsidian bridge.  Transparent to callers: the function either succeeds
   or returns a failure result after retries are exhausted.  Never raises
   on transient failures — safe for MCP gateway use.

2. ``obsidian_retry`` capability — explicit MCP wrapper agents can call
   on any bridge-dependent capability with custom retry params.

Both check bridge health before each attempt, wait between retries,
and log latency context per attempt.

Failure detection (post-CP7)
----------------------------

The decorator catches two kinds of failures:

1. **Typed ObsidianError exceptions** — raised by the bridge layer
   (``write_file_raw``, ``_request_with_status``). The decorator
   classifies via ``isinstance``: terminal subclasses
   (``ObsidianNotRunning``, ``ObsidianPluginMissing``,
   ``ObsidianPluginDisabled``) short-circuit immediately —
   sleeping 60s for a disabled plugin is pure waste. Other typed
   subclasses retry per the wait schedule. On exhaustion the
   decorator translates to a ``bridge_failure(...)`` dict so MCP
   callers see a structured failure rather than a raw exception.

2. **bridge_failure() returns** — legacy protocol where decorated
   functions return a result dict with a ``_bridge_transient`` marker.
   Predates the typed-exception system; supported indefinitely so
   any function that prefers explicit-return over let-it-raise can
   coexist.

Both paths converge on the same retry / short-circuit logic.
"""

from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Callable, TypeVar

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Marker key for standard bridge failure results.  Private — callers
# should use bridge_failure() and is_bridge_failure(), not this key.
_BRIDGE_TRANSIENT_KEY = "_bridge_transient"

# States that are NOT recoverable by waiting / retrying. When the bridge
# state is one of these, @bridge_retry short-circuits after the first
# attempt — sleeping 60s for a disabled plugin helps nobody.
#
# String-form for the legacy bridge_failure() dict path. The typed-
# exception path uses _TERMINAL_OBSIDIAN_ERROR_KINDS below.
_TERMINAL_STATES = frozenset({
    "obsidian_not_running",
    "plugin_not_installed",
    "plugin_disabled",
})

# Same set, in error_kind form. Matched against ObsidianError.error_kind
# to short-circuit the decorator on terminal types raised by the bridge.
_TERMINAL_OBSIDIAN_ERROR_KINDS = frozenset({
    "obsidian_not_running",
    "obsidian_plugin_missing",
    "obsidian_plugin_disabled",
})


def _is_terminal_obsidian_error(exc: BaseException) -> bool:
    """True if the exception represents a state retrying can't fix.

    Imports lazily so this module's import surface stays narrow and
    doesn't pull obsidian.errors during early bootstrap.
    """
    try:
        from work_buddy.obsidian.errors import ObsidianError
    except ImportError:
        return False
    if not isinstance(exc, ObsidianError):
        return False
    return getattr(exc, "error_kind", "") in _TERMINAL_OBSIDIAN_ERROR_KINDS


def _exception_to_bridge_failure(exc: BaseException, fn_name: str) -> dict[str, Any]:
    """Translate a typed ObsidianError to a bridge_failure() dict.

    Used on retry exhaustion to give MCP callers a structured result
    rather than letting the raw exception propagate. Carries the
    error_kind so consumers can distinguish failure categories.
    """
    error_kind = getattr(exc, "error_kind", "obsidian_unknown")
    message = f"{fn_name}: {type(exc).__name__}: {exc}"
    failure = bridge_failure(message)
    # bridge_failure already populates _bridge_state from
    # get_last_bridge_state(); also surface the typed error_kind so
    # downstream consumers (gateway, dashboard, retry queue) can key
    # off the structured signal directly.
    failure["error_kind"] = error_kind
    return failure


# ---------------------------------------------------------------------------
# Standard bridge failure protocol
# ---------------------------------------------------------------------------

def bridge_failure(
    message: str,
    *,
    state: str | None = None,
    state_detail: str | None = None,
) -> dict[str, Any]:
    """Create a standard bridge failure result enriched with four-state diagnostics.

    Use this in any ``@bridge_retry``-decorated function when a bridge
    operation fails (e.g. ``bridge.read_file()`` returns None). The
    decorator detects the marker and retries automatically.

    If ``state`` / ``state_detail`` are omitted, the helper consults
    ``bridge.get_last_bridge_state()`` to auto-classify the failure into
    the four-state taxonomy (obsidian_not_running, timeout,
    plugin_not_installed, plugin_disabled, http_error, unknown). This
    lets callers surface actionable messages without knowing the state
    machine themselves.

    Args:
        message: Human-readable description of what failed.
        state: Optional override for the state label (e.g. if the caller
            already classified the failure).
        state_detail: Optional override for the state explanation.

    Returns:
        ``{"success": False, "message": ..., "_bridge_transient": bool,
        "_bridge_state": str, "_bridge_state_detail": str}``. The
        transient flag is set to ``False`` for terminal states (plugin
        disabled / not installed, Obsidian not running) so the retry
        decorator can short-circuit instead of sleeping uselessly.
    """
    if state is None or state_detail is None:
        try:
            from work_buddy.obsidian.bridge import get_last_bridge_state
            info = get_last_bridge_state()
            state = state or info.get("state") or "unknown"
            state_detail = state_detail or info.get("detail") or ""
        except Exception:
            state = state or "unknown"
            state_detail = state_detail or ""

    return {
        "success": False,
        "message": message,
        _BRIDGE_TRANSIENT_KEY: True,
        "_bridge_state": state,
        "_bridge_state_detail": state_detail,
        "_bridge_terminal": state in _TERMINAL_STATES,
    }


def is_terminal_bridge_failure(result: Any) -> bool:
    """True if the failure is one that retrying will never fix (state 1/3/4).

    Used by ``@bridge_retry`` to short-circuit the wait-and-retry loop
    when the diagnosis is "Obsidian not running", "plugin not
    installed", or "plugin disabled" — states the user must resolve
    out of band.
    """
    return isinstance(result, dict) and result.get("_bridge_terminal") is True


def is_bridge_failure(result: Any) -> bool:
    """Check if a result is a standard bridge failure.

    Definitive check — looks for the ``_bridge_transient`` marker set by
    ``bridge_failure()``.  No string matching, no heuristics.
    """
    return isinstance(result, dict) and result.get(_BRIDGE_TRANSIENT_KEY) is True


# ---------------------------------------------------------------------------
# @bridge_retry decorator — a Retry strategy expressed as a decorator
# ---------------------------------------------------------------------------
#
# ``@bridge_retry`` is a thin shim over the resilience framework: a guarded
# call whose strategy chain is ``RetryStrategy → _BridgeHealthGate → call``.
# The framework owns the retry loop; the decorator configures the chain
# (max_attempts, base wait) and translates the final ``Outcome`` back into
# the decorator's return / raise contract via ``_outcome_to_contract``.
#
# What lives where now:
#   - The retry loop:        ``RetryStrategy`` (outermost in the chain).
#   - The 3-way exception
#     policy:                ``_bridge_classify`` (the ``classify=`` hook):
#                              PWU         -> never reaches it (passthrough,
#                                            re-raised by the seam).
#                              terminal-3  -> TERMINAL_FAILURE (not retryable).
#                              permanent   -> TERMINAL_FAILURE (not retryable).
#                              transient   -> TRANSIENT_FAILURE (retryable).
#   - ``bridge_failure()`` return-dicts:
#                            ``classify_bridge_result`` (the
#                            ``result_classifier=`` hook).
#   - ``is_available()`` health-skip:
#                            ``_BridgeHealthGate``, composed *inside*
#                            ``RetryStrategy`` so it runs once per attempt.
#                            On attempt > 1, if the bridge is unavailable, the
#                            gate re-yields the prior attempt's ``Outcome``
#                            (the prior failure stands) — sleeping while the
#                            bridge is known-down would help nobody, and the
#                            prior attempt's classification is already the
#                            answer the caller will receive.
#   - Final-``Outcome``
#     translation:           ``_outcome_to_contract``.
#
# The wait between attempts is the framework's exponential backoff with
# full jitter (``asyncio.sleep`` inside ``RetryStrategy``). Backoff ceiling
# is ``max(wait_seconds, 30)`` and the first wait samples
# ``uniform(0, wait_seconds)``. Standard Polly v8 / resilience4j / AWS /
# Google SRE pattern — avoids thundering-herd retry pile-ups. Despite the
# parameter name, ``wait_seconds`` is the *base* of the jittered backoff,
# not a fixed wait. The behavioural contract (retry up to N times with a
# wait between) holds.


def _bridge_classify(exc: BaseException) -> Any:
    """Classify a bridge-call exception onto the resilience outcome taxonomy.

    Reproduces ``@bridge_retry``'s three-way exception policy as an
    ``OutcomeKind`` — used as the ``classify=`` hook of the guarded call so
    the emitted telemetry carries an honest outcome kind:

      - a terminal ``ObsidianError`` (Obsidian not running, plugin missing /
        disabled) → ``TERMINAL_FAILURE``;
      - any other permanent error — ``classify_error(exc) != "transient"``,
        which covers ``ObsidianRefused`` and non-Obsidian permanent errors —
        → ``TERMINAL_FAILURE``;
      - everything else → ``TRANSIENT_FAILURE``.

    ``ObsidianPostWriteUncertain`` never reaches here: it is a passthrough
    exception, re-raised by the seam before classification. ``classify_error``
    is imported lazily so a test patching ``work_buddy.errors.classify_error``
    is honoured.
    """
    from work_buddy.errors import classify_error
    from work_buddy.resilience import OutcomeKind

    if _is_terminal_obsidian_error(exc):
        return OutcomeKind.TERMINAL_FAILURE
    if classify_error(exc) != "transient":
        return OutcomeKind.TERMINAL_FAILURE
    return OutcomeKind.TRANSIENT_FAILURE


class _BridgeHealthGate:
    """Resilience strategy reproducing ``@bridge_retry``'s ``is_available()`` skip.

    Composed *inside* ``RetryStrategy`` so it runs once per retry attempt.
    On attempt > 1, if ``bridge.is_available()`` is False, the gate re-yields
    the previous attempt's ``Outcome`` instead of calling the bridge —
    sleeping the next attempt while the bridge is known-down would help
    nobody, and the prior attempt's classification is already what the
    caller will receive on exhaustion.

    One instance per decorated call (created fresh in ``wrapper``), driven by
    a single event loop across its retry attempts — plain instance state is
    safe without a lock.
    """

    def __init__(self, fn_name: str, max_retries: int) -> None:
        self._fn_name = fn_name
        self._max_retries = max_retries
        self._attempt = 0
        self._last_outcome: Any = None

    async def execute(self, nxt: Any, ctx: Any) -> Any:
        self._attempt += 1
        if self._attempt > 1:
            # Lazy imports keep the module load surface narrow and let test
            # patches of these attributes take effect.
            from work_buddy.obsidian.bridge import (
                get_latency_context,
                is_available,
            )
            if not is_available():
                latency = get_latency_context()
                logger.info(
                    "bridge_retry(%s): bridge unavailable before attempt "
                    "%d/%d (%s) — skipping call; prior outcome stands.",
                    self._fn_name, self._attempt, self._max_retries, latency,
                )
                if self._last_outcome is not None:
                    return self._last_outcome
                # Unreachable in practice: a skip only happens on attempt
                # > 1, which only happens after a retryable attempt 1.
                from work_buddy.resilience import Outcome, OutcomeKind
                return Outcome.failure(
                    OutcomeKind.TRANSIENT_FAILURE,
                    detail=f"bridge unavailable; no prior outcome [{latency}]",
                )
        outcome = await nxt(ctx)
        self._last_outcome = outcome
        return outcome


def _outcome_to_contract(outcome: Any, fn_name: str) -> Any:
    """Translate a guarded-call ``Outcome`` back into ``@bridge_retry``'s contract.

    The decorator either returns a value / a ``bridge_failure`` dict, or
    raises. This maps the framework's uniform ``Outcome`` onto that three-way
    return / raise contract — the inverse of ``_bridge_classify`` +
    ``classify_bridge_result``:

      - success                                       → return ``outcome.value``.
      - failure carrying a ``bridge_failure`` dict
        (a result-classified failure)                 → return that dict.
      - failure carrying a terminal ``ObsidianError``
        (Obsidian not running, plugin missing /
         disabled)                                    → translate to a
                                                        ``bridge_failure`` dict
                                                        and return.
      - failure carrying a permanent error
        (``classify_error != "transient"`` — incl.
         ``ObsidianRefused`` and non-Obsidian
         permanent errors)                            → re-raise.
      - failure carrying an exhausted *transient*
        ``ObsidianError``                             → translate to a
                                                        ``bridge_failure`` dict
                                                        and return (CP7 — MCP
                                                        callers see a
                                                        structured shape).
      - failure carrying an exhausted *transient*
        non-Obsidian error                            → re-raise.
    """
    from work_buddy.errors import classify_error
    try:
        from work_buddy.obsidian.errors import ObsidianError
    except ImportError:
        ObsidianError = ()  # type: ignore[assignment,misc]

    if outcome.is_success:
        return outcome.value

    # A bridge_failure() return-dict, surfaced by the result classifier onto
    # outcome.metadata["result"]. RetryStrategy returns the *last* outcome;
    # if that last attempt was a result-classified failure, the dict is what
    # the contract returns (never raises on this path).
    result = outcome.metadata.get("result")
    if is_bridge_failure(result):
        return result

    err = outcome.error
    if err is not None:
        if _is_terminal_obsidian_error(err):
            return _exception_to_bridge_failure(err, fn_name)
        if classify_error(err) != "transient":
            raise err  # permanent (incl. ObsidianRefused) — don't retry
        if isinstance(err, ObsidianError):
            return _exception_to_bridge_failure(err, fn_name)  # CP7
        raise err  # exhausted transient non-Obsidian — re-raise

    # Defensive: a failure outcome with neither result nor error (e.g. a
    # REJECTED deadline gate). The decorator never raises on this path.
    return bridge_failure(
        f"bridge_retry({fn_name}): {outcome.kind.value} [{outcome.detail}]"
    )


def bridge_retry(
    max_retries: int = 3,
    wait_seconds: int = 60,
) -> Callable[[F], F]:
    """Decorator: retry a bridge-dependent function on transient failures.

    Apply to any function that depends on the Obsidian bridge. The decorator
    is a thin shim over the resilience framework: each call becomes a guarded
    call whose strategy chain is
    ``RetryStrategy → _BridgeHealthGate → call``. Two failure modes are
    handled transparently:

    1. **Exceptions** — a transient error (timeout, connection refused) is
       waited out and retried; a permanent error is re-raised immediately;
       a terminal ``ObsidianError`` (Obsidian not running, plugin missing /
       disabled) short-circuits to a ``bridge_failure`` dict without waiting.

    2. **bridge_failure() returns** — a result created by ``bridge_failure()``
       is treated like a transient exception: wait, health-check, retry; a
       terminal bridge failure short-circuits.

    On success the result is returned transparently. On exhaustion:

    - Exception path → re-raises the last exception (a typed *transient*
      ``ObsidianError`` is instead translated to a ``bridge_failure`` dict,
      so MCP callers see a structured result).
    - bridge_failure path → returns the last failure result (never raises).

    ``ObsidianPostWriteUncertain`` is a passthrough: it propagates immediately
    (no retry, no wait) so the gateway's verify-then-decide path can run.

    Decorated functions stay safe for MCP gateway use — a transient bridge
    outage can never crash the gateway process. Works with the
    ``requires=["obsidian"]`` gateway check: that check catches "obsidian not
    running at all" at dispatch time, while this decorator catches transient
    failures *during* execution.

    Cadence note: the wait between attempts is the framework's exponential
    backoff with full jitter (``asyncio.sleep`` inside ``RetryStrategy``),
    not a fixed ``wait_seconds``. Backoff ceiling is
    ``max(wait_seconds, 30)`` and the first wait is sampled from
    ``uniform(0, wait_seconds)``. Standard Polly v8 / resilience4j / AWS /
    Google SRE pattern; avoids thundering-herd retry pile-ups.

    Usage::

        @bridge_retry(max_retries=3, wait_seconds=60)
        def task_create(task_text, ...):
            content = bridge.read_file(fp)
            if content is None:
                return bridge_failure(f"Could not read {fp}")
            ...
    """
    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # One-retry-layer rule: if a guarded call is already running on
            # this thread (an outer guarded call, or an async caller), that
            # layer owns resilience for this call — and ``guarded_call_sync``
            # refuses to run inside a live event loop anyway. Call ``fn``
            # bare and let the outer layer classify / retry.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                return fn(*args, **kwargs)

            from work_buddy.obsidian.resilient_bridge import (
                classify_bridge_result,
            )
            from work_buddy.resilience import RetryStrategy, guarded_call_sync

            try:
                from work_buddy.obsidian.errors import (
                    ObsidianPostWriteUncertain,
                )
                passthrough: tuple[type[BaseException], ...] = (
                    ObsidianPostWriteUncertain,
                )
            except ImportError:
                passthrough = ()

            strategies = [
                RetryStrategy(
                    max_attempts=max_retries,
                    base_delay_s=float(wait_seconds),
                    max_delay_s=max(float(wait_seconds), 30.0),
                ),
                _BridgeHealthGate(fn.__name__, max_retries),
            ]

            def _call() -> Any:
                return fn(*args, **kwargs)

            outcome = guarded_call_sync(
                f"bridge_retry:{fn.__name__}",
                _call,
                strategies=strategies,
                classify=_bridge_classify,
                result_classifier=classify_bridge_result,
                passthrough_exceptions=passthrough,
            )
            return _outcome_to_contract(outcome, fn.__name__)

        return wrapper  # type: ignore[return-value]
    return decorator


# ---------------------------------------------------------------------------
# obsidian_retry capability
# ---------------------------------------------------------------------------

def obsidian_retry(
    operation_id: str,
    max_retries: int = 3,
    wait_seconds: int = 60,
) -> dict[str, Any]:
    """Synchronous bridge-aware retry for a previously recorded operation.

    Unlike ``@bridge_retry`` (decorator, applied at definition time), this
    is an explicit MCP capability agents can call to replay a bridge-
    dependent operation that failed or timed out — typically after a
    consent timeout or an Obsidian bridge hiccup.

    Health-checks the bridge before each attempt, waits between retries,
    and captures latency context per attempt.

    The capability name and parameters are loaded from the operation
    record; agents do not re-supply them. If you don't have an
    ``operation_id`` you don't need retry — just call the underlying
    capability directly; the gateway's automatic background retry
    handles transient bridge hiccups on fresh calls.

    Args:
        operation_id: The operation_id from a previous failed or timed-
            out call. Included in `wb_run` / `consent_request` timeout
            returns, and visible via `wb_status()`.
        max_retries: Maximum number of attempts (default: 3).
        wait_seconds: Seconds to wait between attempts (default: 60).

    Returns:
        The capability's result on success, or a bridge_failure dict on
        exhaustion.
    """
    from work_buddy.mcp_server.registry import get_registry
    from work_buddy.obsidian.bridge import is_available, get_latency_context
    from work_buddy.errors import classify_error
    from work_buddy.mcp_server.tools.gateway import _load_operation

    if not operation_id:
        return {
            "success": False,
            "error": "obsidian_retry requires an 'operation_id'.",
        }

    record = _load_operation(operation_id)
    if record is None:
        return {
            "success": False,
            "error": f"Operation '{operation_id}' not found",
        }

    capability = record.get("name")
    params = record.get("params") or {}
    originating_session_id = record.get("originating_session_id")

    if not capability:
        return {
            "success": False,
            "error": (
                f"Operation '{operation_id}' is missing a capability name "
                f"in its record — cannot replay."
            ),
        }

    registry = get_registry()
    entry = registry.get(capability)

    if entry is None:
        return {"success": False, "error": f"Capability '{capability}' not found"}

    last_failure: dict[str, Any] | None = None
    last_exc: Exception | None = None

    for attempt in range(1, max_retries + 1):
        if attempt > 1 and not is_available():
            latency = get_latency_context()
            logger.info(
                "obsidian_retry(%s): bridge unavailable before attempt "
                "%d/%d (%s). Waiting %ds...",
                capability, attempt, max_retries, latency, wait_seconds,
            )
            if attempt < max_retries:
                time.sleep(wait_seconds)
                continue
            else:
                if last_failure is not None:
                    return last_failure
                return bridge_failure(
                    f"Bridge unavailable after {max_retries} attempts "
                    f"[{latency}]"
                )

        try:
            # Pin the originating session on the ContextVar so the
            # @requires_consent decorator's is_granted check resolves
            # to the agent's session DB rather than the MCP server's
            # bootstrap session. Without this, replays of consent-
            # gated ops can synchronously fail with ConsentRequired
            # even when the user has approved out-of-band.
            if originating_session_id:
                from work_buddy.agent_session import (
                    set_originating_session, reset_originating_session,
                )
                _orig_token = set_originating_session(originating_session_id)
            else:
                _orig_token = None
            try:
                result = entry.callable(**params)
            finally:
                if _orig_token is not None:
                    reset_originating_session(_orig_token)
        except Exception as exc:
            # CP-A6: ObsidianPostWriteUncertain demands verify-then-decide,
            # not blind retry. Propagate so the gateway's CP5 verify path
            # decides whether the underlying write actually landed.
            try:
                from work_buddy.obsidian.errors import ObsidianPostWriteUncertain
            except ImportError:
                ObsidianPostWriteUncertain = ()  # type: ignore[misc,assignment]
            if isinstance(exc, ObsidianPostWriteUncertain):
                logger.info(
                    "obsidian_retry(%s): propagating "
                    "ObsidianPostWriteUncertain (path=%r, write_mode=%r) "
                    "to gateway for verify-then-decide.",
                    capability,
                    getattr(exc, "path", "?"),
                    getattr(exc, "write_mode", "?"),
                )
                raise

            # CP7: terminal ObsidianError subclasses short-circuit. Same
            # rationale as the @bridge_retry decorator above.
            if _is_terminal_obsidian_error(exc):
                logger.info(
                    "obsidian_retry(%s): terminal ObsidianError '%s' "
                    "on attempt %d/%d — short-circuiting.",
                    capability, getattr(exc, "error_kind", ""),
                    attempt, max_retries,
                )
                return _exception_to_bridge_failure(exc, capability)

            error_class = classify_error(exc)
            latency = get_latency_context()
            logger.warning(
                "obsidian_retry(%s): attempt %d/%d raised (%s): %s [%s]",
                capability, attempt, max_retries,
                error_class, exc, latency,
            )
            last_exc = exc

            if error_class != "transient":
                # Build a structured response — include error_kind for
                # typed exceptions so the dashboard / consumer keys off
                # the structured signal rather than the message.
                resp: dict[str, Any] = {"success": False, "error": str(exc)}
                kind = getattr(exc, "error_kind", None)
                if isinstance(kind, str):
                    resp["error_kind"] = kind
                return resp

            if attempt < max_retries:
                time.sleep(wait_seconds)
            else:
                # Translate typed exceptions to bridge_failure dict for
                # consistency with the @bridge_retry decorator's exhaustion
                # path; non-Obsidian transient exceptions stay as a basic
                # error dict.
                try:
                    from work_buddy.obsidian.errors import ObsidianError
                except ImportError:
                    ObsidianError = ()  # type: ignore[assignment]
                if isinstance(exc, ObsidianError):
                    return _exception_to_bridge_failure(exc, capability)
                return {"success": False, "error": str(exc)}
            continue

        if is_bridge_failure(result):
            latency = get_latency_context()
            logger.warning(
                "obsidian_retry(%s): attempt %d/%d returned "
                "bridge_failure: %s [%s]",
                capability, attempt, max_retries,
                result.get("message", ""), latency,
            )
            last_failure = result

            # Short-circuit on terminal states (plugin disabled / not
            # installed / Obsidian not running).
            if is_terminal_bridge_failure(result):
                logger.info(
                    "obsidian_retry(%s): terminal state '%s' — "
                    "skipping remaining retries.",
                    capability, result.get("_bridge_state"),
                )
                return result

            if attempt < max_retries:
                time.sleep(wait_seconds)
            else:
                return result
            continue

        # Success
        return result

    # Safety net
    if last_failure is not None:
        return last_failure
    if last_exc is not None:
        return {"success": False, "error": str(last_exc)}
    return bridge_failure("Unexpected exhaustion")
