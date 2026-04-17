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

Standard bridge failure protocol
---------------------------------

Functions decorated with ``@bridge_retry`` signal retriable failures by
returning ``bridge_failure("reason")``.  This produces a dict with a
``_bridge_transient`` marker that the decorator checks definitively —
no string matching, no heuristics.

::

    @bridge_retry()
    def my_function():
        content = bridge.read_file(fp)
        if content is None:
            return bridge_failure(f"Could not read {fp}")
        ...

The decorator retries on ``bridge_failure`` returns and on transient
exceptions (ConnectionError, TimeoutError, etc.).  On exhaustion it
returns the last failure result — never raises for bridge issues.
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


# ---------------------------------------------------------------------------
# Standard bridge failure protocol
# ---------------------------------------------------------------------------

def bridge_failure(message: str) -> dict[str, Any]:
    """Create a standard transient bridge failure result.

    Use this in any ``@bridge_retry``-decorated function when a bridge
    operation fails (e.g. ``bridge.read_file()`` returns None).  The
    decorator detects the marker and retries automatically.

    Args:
        message: Human-readable description of what failed.

    Returns:
        ``{"success": False, "message": ..., "_bridge_transient": True}``
    """
    return {
        "success": False,
        "message": message,
        _BRIDGE_TRANSIENT_KEY: True,
    }


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
            error_class = classify_error(exc)
            latency = get_latency_context()
            logger.warning(
                "obsidian_retry(%s): attempt %d/%d raised (%s): %s [%s]",
                capability, attempt, max_retries,
                error_class, exc, latency,
            )
            last_exc = exc

            if error_class != "transient":
                return {"success": False, "error": str(exc)}

            if attempt < max_retries:
                time.sleep(wait_seconds)
            else:
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
