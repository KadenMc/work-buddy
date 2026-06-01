"""Error classification for retry queue decisions.

Centralizes the logic for determining whether a failure is transient
(worth retrying automatically) or permanent (no point retrying).

This module is intentionally lightweight — no heavy imports at module
scope. Typed Obsidian exceptions are imported lazily inside
:func:`classify_error` to avoid a circular dependency
(``work_buddy.obsidian.errors`` could grow callers that pull this module).
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Transient error patterns
# ---------------------------------------------------------------------------
# Strings that, when found in an error message, indicate a transient failure.
# Case-insensitive matching.
#
# Post-CP9 this list is purely a fallback for NON-Obsidian transient failures
# (LM Studio, embedding service, sidecar HTTP, etc.). Obsidian failures
# take the typed-exception fast-path (isinstance check on ObsidianError);
# nothing inside this list is needed for the Obsidian bridge anymore.
# Removed in CP9: "bridge", "editor_dirty", "urlopen error", "winerror 10061"
# — all subsumed by the typed-exception path.

_TRANSIENT_PATTERNS: tuple[str, ...] = (
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "service unavailable",
    "unreachable",
    "errno 111",         # Linux connection refused
    "errno 104",         # Linux connection reset
)

# Error-kind values (the structured signal carried by ObsidianError
# subclasses) that mean "transient — worth retrying." All Obsidian
# error_kinds are transient EXCEPT obsidian_refused (4xx-other-than-409;
# the request will never succeed without changing).
_TRANSIENT_OBSIDIAN_KINDS: frozenset[str] = frozenset({
    "obsidian_unknown",            # generic — assume transient
    "obsidian_unreachable",        # ambiguous connectivity — assume transient
    "obsidian_startup_race",       # plugin still binding the port — will clear
    "obsidian_timeout",
    "obsidian_post_write_uncertain",
    "obsidian_http_error",         # generic — assume transient
    "obsidian_editor_conflict",
    "obsidian_server_error",
})

# "User must act out of band" failures: retrying without the user doing
# something (open Obsidian, install / enable the plugin, fix a 4xx-refused
# request) never succeeds. They are NOT auto-enqueued — a deliberately-closed
# Obsidian fails fast instead of churning 5 futile retries and emitting an
# exhaustion notification. A *transiently* unavailable bridge (startup race,
# timeout, 5xx) stays transient above and still auto-recovers via the queue.
_PERMANENT_OBSIDIAN_KINDS: frozenset[str] = frozenset({
    "obsidian_refused",          # 4xx other than 409 — structural refusal
    "obsidian_not_running",      # Obsidian app closed — user must open it
    "obsidian_plugin_missing",   # plugin not installed — user must install
    "obsidian_plugin_disabled",  # plugin disabled — user must enable
})

# Exception class names (not types) that are always transient. The
# typed-exception fast-path (isinstance check on ObsidianError) at the
# top of classify_error covers the Obsidian hierarchy; this list is for
# any legacy non-Obsidian named exceptions that should classify transient
# without needing a type import.
_TRANSIENT_EXCEPTION_NAMES: tuple[str, ...] = (
    "ObsidianEditorConflict",    # safety-net if isinstance check skipped
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


def _load_obsidian_error_types():
    """Lazy-import ObsidianError + ObsidianRefused to avoid module cycle.

    Returns a tuple ``(ObsidianError, ObsidianRefused)`` or ``(None, None)``
    if the import fails (e.g. during early bootstrap). When None, the
    isinstance fast-path is skipped and we fall back to name + message
    matching — still correct, just less precise.
    """
    try:
        from work_buddy.obsidian.errors import ObsidianError, ObsidianRefused
        return ObsidianError, ObsidianRefused
    except ImportError:
        return None, None


def classify_error(exc: Exception) -> str:
    """Classify an exception as transient, permanent, or unknown.

    Returns:
        "transient" — worth retrying (timeouts, connection issues, service hiccups)
        "permanent" — will never succeed on retry (type errors, missing args, etc.)
        "unknown"   — can't tell; default to no auto-retry

    Resolution order:
      1. Typed ObsidianError isinstance check (CP3 fast-path)
      2. Type-based: TimeoutError / ConnectionRefusedError / ...
      3. Name-based: TRANSIENT_EXCEPTION_NAMES / PERMANENT_EXCEPTION_NAMES
      4. Message-pattern matching (legacy fallback)
    """
    # Fast path: typed ObsidianError. Classify by the canonical ``error_kind``
    # — the same signal the result-dict path (is_transient_result) keys on, so
    # a raised exception and a ``bridge_failure`` return classify identically.
    # "User must act" kinds (Obsidian closed, plugin missing/disabled, 4xx
    # refused) are permanent — no auto-retry; everything else is transient.
    ObsidianError, _ = _load_obsidian_error_types()
    if ObsidianError is not None and isinstance(exc, ObsidianError):
        kind = getattr(exc, "error_kind", "obsidian_unknown")
        return "permanent" if kind in _PERMANENT_OBSIDIAN_KINDS else "transient"

    # Check exception type first
    if isinstance(exc, _TRANSIENT_EXCEPTION_TYPES):
        return "transient"

    exc_type_name = type(exc).__name__
    if exc_type_name in _TRANSIENT_EXCEPTION_NAMES:
        return "transient"
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
            # CP9: also check whether the inner exception is itself a
            # transient type (ConnectionRefusedError, TimeoutError, ...).
            # Pre-CP9 the trimmed _TRANSIENT_PATTERNS list contained
            # 'urlopen error' so the OUTER message matched; with that
            # gone, we need to recurse into the inner exception properly.
            if isinstance(reason, _TRANSIENT_EXCEPTION_TYPES):
                return "transient"
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

    Many capabilities return ``{"error": "..."}`` or ``{"success": False, ...}``
    instead of raising. This decides whether the failure looks transient.

    Resolution order (post-CP3):
      1. ``result["error_kind"]`` — the structured signal carried by typed
         ObsidianError instances. Always wins when present; substring
         matching never even runs. CP4 ensures the gateway populates
         this when an ObsidianError is caught.
      2. Legacy ``error`` / ``message`` string-pattern matching for
         non-Obsidian callers and pre-typed-exception code paths.

    Also handles the bridge pattern where operations return None on
    failure (though None results typically don't reach the gateway as
    errors).
    """
    if result is None:
        return True

    if not isinstance(result, dict):
        return False

    # Fast path: structured error_kind from typed exception. Wins over
    # any string-matching — gateway populates this in CP4.
    error_kind = result.get("error_kind")
    if isinstance(error_kind, str):
        if error_kind in _PERMANENT_OBSIDIAN_KINDS:
            return False
        if error_kind in _TRANSIENT_OBSIDIAN_KINDS:
            return True
        # Unknown error_kind — fall through to message matching as a
        # safety net rather than guessing.

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
