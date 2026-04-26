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
# @bridge_retry decorator
# ---------------------------------------------------------------------------

def bridge_retry(
    max_retries: int = 3,
    wait_seconds: int = 60,
) -> Callable[[F], F]:
    """Decorator: retry a function on transient bridge failures.

    Apply to any function that depends on the Obsidian bridge.  Handles
    two failure modes transparently:

    1. **Exceptions** — if the function raises a transient error (timeout,
       connection refused), the decorator waits, health-checks, and retries.
       Permanent errors are re-raised immediately.

    2. **bridge_failure() returns** — if the function returns a result
       created by ``bridge_failure()``, the decorator treats it the same
       as a transient exception: wait, health-check, retry.

    On success the result is returned transparently.  On exhaustion:

    - Exception path → re-raises the last exception
    - bridge_failure path → returns the last failure result (never raises)

    This means decorated functions remain safe for MCP gateway use — a
    transient bridge outage can never crash the gateway process.

    Works with the ``requires=["obsidian"]`` gateway check: that check
    catches "obsidian not running at all" at dispatch time, while this
    decorator catches transient failures *during* execution.

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
            from work_buddy.obsidian.bridge import is_available, get_latency_context
            from work_buddy.errors import classify_error

            last_exc: Exception | None = None
            last_failure: dict[str, Any] | None = None

            for attempt in range(1, max_retries + 1):
                # Check bridge health before each attempt (except the first
                # — the gateway's requires check already verified it)
                if attempt > 1 and not is_available():
                    latency = get_latency_context()
                    logger.info(
                        "bridge_retry(%s): bridge unavailable before attempt "
                        "%d/%d (%s). Waiting %ds...",
                        fn.__name__, attempt, max_retries, latency,
                        wait_seconds,
                    )
                    if attempt < max_retries:
                        time.sleep(wait_seconds)
                        continue
                    else:
                        # Exhausted waiting for bridge.
                        if last_failure is not None:
                            return last_failure
                        if last_exc is not None:
                            raise last_exc
                        return bridge_failure(
                            f"Bridge unavailable after {max_retries} "
                            f"attempts [{latency}]"
                        )

                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    # CP7: terminal ObsidianError subclasses short-circuit
                    # without sleeping. Sleeping 60s for a missing plugin
                    # or a Obsidian-not-running state is pure waste — the
                    # user must act out of band (open Obsidian, install/
                    # enable the plugin) before any retry could succeed.
                    if _is_terminal_obsidian_error(exc):
                        logger.info(
                            "bridge_retry(%s): terminal ObsidianError '%s' "
                            "on attempt %d/%d — short-circuiting "
                            "(translating to bridge_failure dict).",
                            fn.__name__, getattr(exc, "error_kind", ""),
                            attempt, max_retries,
                        )
                        return _exception_to_bridge_failure(exc, fn.__name__)

                    error_class = classify_error(exc)
                    latency = get_latency_context()
                    logger.warning(
                        "bridge_retry(%s): attempt %d/%d raised (%s): %s [%s]",
                        fn.__name__, attempt, max_retries,
                        error_class, exc, latency,
                    )
                    last_exc = exc

                    if error_class != "transient":
                        raise  # permanent error — don't retry

                    if attempt < max_retries:
                        time.sleep(wait_seconds)
                    else:
                        # CP7: on exhaustion of a typed ObsidianError,
                        # translate to bridge_failure dict so MCP
                        # callers see structured failure shape rather
                        # than a raw exception. Non-Obsidian transient
                        # exceptions still raise (gateway classifies).
                        try:
                            from work_buddy.obsidian.errors import ObsidianError
                        except ImportError:
                            ObsidianError = ()  # type: ignore[assignment]
                        if isinstance(exc, ObsidianError):
                            return _exception_to_bridge_failure(exc, fn.__name__)
                        raise  # exhausted — let gateway handle it
                    continue

                # Function returned — check for standard bridge failure
                if is_bridge_failure(result):
                    latency = get_latency_context()
                    logger.warning(
                        "bridge_retry(%s): attempt %d/%d returned "
                        "bridge_failure: %s [%s]",
                        fn.__name__, attempt, max_retries,
                        result.get("message", ""), latency,
                    )
                    last_failure = result

                    # Short-circuit on terminal states — sleeping 60s for
                    # a disabled plugin is pure waste. The user must act
                    # (open Obsidian, enable the plugin, install it).
                    if is_terminal_bridge_failure(result):
                        logger.info(
                            "bridge_retry(%s): terminal state '%s' — "
                            "skipping remaining retries.",
                            fn.__name__, result.get("_bridge_state"),
                        )
                        return result

                    if attempt < max_retries:
                        time.sleep(wait_seconds)
                    else:
                        return result  # exhausted — return failure (never raise)
                    continue

                # Success
                return result

            # Safety net
            if last_failure is not None:
                return last_failure
            if last_exc is not None:
                raise last_exc
            return bridge_failure(
                f"bridge_retry({fn.__name__}): unexpected exhaustion"
            )

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
            result = entry.callable(**params)
        except Exception as exc:
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
