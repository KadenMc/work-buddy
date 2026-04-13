"""Error classification for retry queue decisions.

Centralizes the logic for determining whether a failure is transient
(worth retrying automatically) or permanent (no point retrying).

This module is intentionally lightweight — no heavy imports.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Transient error patterns
# ---------------------------------------------------------------------------
# Strings that, when found in an error message, indicate a transient failure.
# Case-insensitive matching.

_TRANSIENT_PATTERNS: tuple[str, ...] = (
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "service unavailable",
    "unreachable",
    "bridge",            # Obsidian bridge failures
    "urlopen error",     # urllib failures
    "winerror 10061",    # Windows connection refused
    "errno 111",         # Linux connection refused
    "errno 104",         # Linux connection reset
)

# Exception types that are always transient (regardless of message).
_TRANSIENT_EXCEPTION_TYPES: tuple[type, ...] = (
    TimeoutError,
    ConnectionRefusedError,
    ConnectionResetError,
    ConnectionAbortedError,
)

# Exception types that are always permanent (never worth retrying).
_PERMANENT_EXCEPTION_NAMES: tuple[str, ...] = (
    "TypeError",
    "KeyError",
    "ValueError",
    "AttributeError",
    "ImportError",
    "ModuleNotFoundError",
    "SyntaxError",
    "ConsentRequired",
    "ToolUnavailable",
    "PermissionError",
    "FileNotFoundError",
)


def classify_error(exc: Exception) -> str:
    """Classify an exception as transient, permanent, or unknown.

    Returns:
        "transient" — worth retrying (timeouts, connection issues, service hiccups)
        "permanent" — will never succeed on retry (type errors, missing args, etc.)
        "unknown"   — can't tell; default to no auto-retry
    """
    # Check exception type first
    if isinstance(exc, _TRANSIENT_EXCEPTION_TYPES):
        return "transient"

    exc_type_name = type(exc).__name__
    if exc_type_name in _PERMANENT_EXCEPTION_NAMES:
        return "permanent"

    # Check the exception message for transient patterns
    msg = str(exc).lower()
    for pattern in _TRANSIENT_PATTERNS:
        if pattern in msg:
            return "transient"

    # URLError often wraps a transient inner error
    if exc_type_name == "URLError":
        reason = getattr(exc, "reason", None)
        if reason is not None:
            inner_msg = str(reason).lower()
            for pattern in _TRANSIENT_PATTERNS:
                if pattern in inner_msg:
                    return "transient"

    # RuntimeError is ambiguous — check message
    if isinstance(exc, RuntimeError):
        for pattern in _TRANSIENT_PATTERNS:
            if pattern in msg:
                return "transient"
        # RuntimeError("Obsidian bridge not available") is transient
        if "not available" in msg or "not running" in msg:
            return "transient"

    # OSError subtypes not already caught
    if isinstance(exc, OSError):
        for pattern in _TRANSIENT_PATTERNS:
            if pattern in msg:
                return "transient"

    return "unknown"


def is_transient_result(result: Any) -> bool:
    """Check if a capability's return value indicates a transient failure.

    Many capabilities return {"error": "..."} or {"success": False, ...}
    instead of raising. This checks whether the error string looks transient.

    Also handles the bridge pattern where operations return None on failure
    (though None results typically don't reach the gateway as errors).
    """
    if result is None:
        return True

    if not isinstance(result, dict):
        return False

    error = result.get("error")
    if not error:
        # Check for {"success": False, "message": "..."} pattern
        if result.get("success") is False:
            error = result.get("message", "")
        else:
            return False

    error_lower = str(error).lower()
    for pattern in _TRANSIENT_PATTERNS:
        if pattern in error_lower:
            return True

    return False


def compute_retry_delay(
    attempt: int,
    strategy: str = "adaptive",
) -> int:
    """Compute the delay in seconds before the next retry attempt.

    Strategies:
        "fixed_10s"   — constant 10s delay (good for brief hiccups)
        "exponential"  — 10s, 20s, 40s, 80s, capped at 120s
        "adaptive"     — starts fast (10s), escalates to longer waits:
                         10s, 20s, 45s, 90s, 120s (capped)
                         Designed for Obsidian-style outages where it
                         might be a 1s spike OR a 5-minute restart.

    Args:
        attempt: Current attempt number (1-based; attempt=1 means first retry)
        strategy: Backoff strategy name
    """
    if strategy == "fixed_10s":
        return 10
    elif strategy == "exponential":
        return min(10 * (2 ** (attempt - 1)), 120)
    elif strategy == "adaptive":
        # Ramp: 10s, 20s, 45s, 90s, 120s, 120s, ...
        schedule = [10, 20, 45, 90, 120]
        idx = min(attempt - 1, len(schedule) - 1)
        return schedule[idx]
    else:
        return 10  # fallback
