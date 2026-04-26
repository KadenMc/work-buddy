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
# Post-CP3 these patterns are a FALLBACK for non-Obsidian capabilities and
# legacy callers that haven't migrated to typed exceptions / structured
# error_kind. Obsidian failures take the typed-exception fast-path
# (isinstance check) and never reach the pattern list. CP9 trims the
# Obsidian-specific patterns out of this list once the migration is done.

_TRANSIENT_PATTERNS: tuple[str, ...] = (
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "service unavailable",
    "unreachable",
    "bridge",            # Obsidian bridge failures (legacy; CP9 removes)
    "urlopen error",     # urllib failures
    "winerror 10061",    # Windows connection refused
    "errno 111",         # Linux connection refused
    "errno 104",         # Linux connection reset
    "editor_dirty",      # Obsidian editor-conflict (legacy; CP9 removes)
)

# Error-kind values (the structured signal carried by ObsidianError
# subclasses) that mean "transient — worth retrying." All Obsidian
# error_kinds are transient EXCEPT obsidian_refused (4xx-other-than-409;
# the request will never succeed without changing).
_TRANSIENT_OBSIDIAN_KINDS: frozenset[str] = frozenset({
    "obsidian_unknown",            # generic — assume transient
    "obsidian_unreachable",
    "obsidian_not_running",
    "obsidian_plugin_missing",
    "obsidian_plugin_disabled",
    "obsidian_startup_race",
    "obsidian_timeout",
    "obsidian_post_write_uncertain",
    "obsidian_http_error",         # generic — assume transient
    "obsidian_editor_conflict",
    "obsidian_server_error",
})

_PERMANENT_OBSIDIAN_KINDS: frozenset[str] = frozenset({
    "obsidian_refused",  # 4xx other than 409 — structural refusal
})

# Exception class names (not types) that are always transient. Name-based
# matching keeps this fallback path dependency-free for non-Obsidian callers.
# Both the legacy "EditorConflict" name and the new "ObsidianEditorConflict"
# are listed during the transition (CP9 removes the legacy entry).
_TRANSIENT_EXCEPTION_NAMES: tuple[str, ...] = (
    "EditorConflict",            # legacy alias (removed in CP9)
    "ObsidianEditorConflict",    # new typed name
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
    # Fast path: typed ObsidianError. ObsidianRefused is the only
    # permanent kind; everything else under ObsidianError is transient.
    ObsidianError, ObsidianRefused = _load_obsidian_error_types()
    if ObsidianError is not None and isinstance(exc, ObsidianError):
        if ObsidianRefused is not None and isinstance(exc, ObsidianRefused):
            return "permanent"
        return "transient"

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
