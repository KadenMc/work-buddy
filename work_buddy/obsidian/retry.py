"""Bridge-aware retry for Obsidian-dependent operations.

Two mechanisms:

1. ``@bridge_retry`` decorator — apply to functions that write to the
   Obsidian vault.  Transparent to callers: the function either succeeds
   or raises after retries are exhausted.

2. ``obsidian_retry`` capability — explicit MCP wrapper agents can call
   on any bridge-dependent capability with custom retry params.

Both check bridge health before each attempt, wait between retries,
and log latency context per attempt.
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable, TypeVar

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


# ---------------------------------------------------------------------------
# @bridge_retry decorator
# ---------------------------------------------------------------------------

def bridge_retry(
    max_retries: int = 3,
    wait_seconds: int = 60,
) -> Callable[[F], F]:
    """Decorator: retry a function on transient bridge failures.

    Apply to functions that write to the Obsidian vault.  The decorated
    function is called normally; if it raises a transient error (timeout,
    connection refused, bridge unavailable), the decorator waits, checks
    bridge health, and retries.

    On success the result is returned transparently — no wrapper dict,
    no extra metadata.  On exhaustion the last exception is re-raised so
    the gateway's normal error handling (background retry queue, error
    classification) still applies.

    Works with the ``requires=["obsidian"]`` gateway check: that check
    catches "obsidian not running at all" at dispatch time, while this
    decorator catches transient failures *during* execution.

    Usage::

        @bridge_retry(max_retries=3, wait_seconds=60)
        def task_create(task_text, ...):
            ...
    """
    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            from work_buddy.obsidian.bridge import is_available, get_latency_context
            from work_buddy.errors import classify_error

            last_exc: Exception | None = None

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
                        # Exhausted — re-raise last exception if we have one,
                        # otherwise raise a RuntimeError
                        if last_exc is not None:
                            raise last_exc
                        raise RuntimeError(
                            f"Bridge unavailable after {max_retries} attempts "
                            f"[{latency}]"
                        )

                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    error_class = classify_error(exc)
                    latency = get_latency_context()
                    logger.warning(
                        "bridge_retry(%s): attempt %d/%d failed (%s): %s [%s]",
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

            # Should not reach here, but safety net
            if last_exc is not None:
                raise last_exc
            raise RuntimeError(f"bridge_retry({fn.__name__}): unexpected exhaustion")

        return wrapper  # type: ignore[return-value]
    return decorator


def obsidian_retry(
    capability: str,
    params: dict[str, Any] | str | None = None,
    max_retries: int = 3,
    wait_seconds: int = 60,
) -> dict[str, Any]:
    """Retry a bridge-dependent capability with health checks between attempts.

    Args:
        capability: Name of the registered capability (e.g. ``"task_create"``).
        params: Parameters for the capability (dict or JSON string).
        max_retries: Maximum number of attempts (including the first).
        wait_seconds: Seconds to wait between attempts.

    Returns:
        On success: ``{"success": True, "result": <result>, "attempts": N}``
        On exhaustion: ``{"success": False, "attempts": N, "last_error": "...",
                         "latency_context": "..."}``
    """
    from work_buddy.mcp_server import registry
    from work_buddy.obsidian.bridge import is_available, get_latency_context
    from work_buddy.errors import classify_error, is_transient_result

    # Parse params if JSON string
    if isinstance(params, str):
        import json
        try:
            params = json.loads(params)
        except (json.JSONDecodeError, TypeError):
            params = {}
    if params is None:
        params = {}

    entry = registry.get_entry(capability)
    if entry is None:
        return {"success": False, "error": f"Unknown capability: {capability!r}", "attempts": 0}

    last_error = ""
    for attempt in range(1, max_retries + 1):
        # Check bridge health before each attempt
        if not is_available():
            latency = get_latency_context()
            logger.info(
                "obsidian_retry: bridge unavailable before attempt %d/%d "
                "(%s). Waiting %ds...",
                attempt, max_retries, latency, wait_seconds,
            )
            if attempt < max_retries:
                time.sleep(wait_seconds)
                continue
            else:
                return {
                    "success": False,
                    "attempts": attempt,
                    "last_error": "Bridge unavailable after all retries",
                    "latency_context": latency,
                }

        # Attempt the operation
        try:
            result = entry.callable(**params)
        except Exception as exc:
            error_str = f"{type(exc).__name__}: {exc}"
            error_class = classify_error(exc)
            latency = get_latency_context()
            logger.warning(
                "obsidian_retry: attempt %d/%d failed (%s): %s [%s]",
                attempt, max_retries, error_class, error_str, latency,
            )
            last_error = error_str

            if error_class != "transient":
                # Permanent error — no point retrying
                return {
                    "success": False,
                    "attempts": attempt,
                    "last_error": error_str,
                    "error_class": "permanent",
                    "latency_context": latency,
                }

            if attempt < max_retries:
                time.sleep(wait_seconds)
                continue
            else:
                return {
                    "success": False,
                    "attempts": attempt,
                    "last_error": error_str,
                    "latency_context": latency,
                }

        # Check for soft transient failures in the result
        if is_transient_result(result):
            result_err = ""
            if isinstance(result, dict):
                result_err = result.get("error", result.get("message", "transient failure"))
            latency = get_latency_context()
            logger.warning(
                "obsidian_retry: attempt %d/%d returned transient result: %s [%s]",
                attempt, max_retries, result_err, latency,
            )
            last_error = str(result_err)

            if attempt < max_retries:
                time.sleep(wait_seconds)
                continue
            else:
                return {
                    "success": False,
                    "attempts": attempt,
                    "last_error": last_error,
                    "result": result,
                    "latency_context": latency,
                }

        # Success
        logger.info(
            "obsidian_retry: attempt %d/%d succeeded [%s]",
            attempt, max_retries, get_latency_context(),
        )
        return {
            "success": True,
            "result": result,
            "attempts": attempt,
        }

    # Should not reach here, but safety net
    return {
        "success": False,
        "attempts": max_retries,
        "last_error": last_error or "Unknown error",
        "latency_context": get_latency_context(),
    }
