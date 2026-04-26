"""Lazy auto-recovery for disabled capabilities.

When the sidecar starts and a tool's first probe fails (or hasn't run
yet), capabilities depending on that tool get filtered into
``DISABLED_CAPABILITIES`` by the registry filter pass. If the probe
later recovers, those capabilities stay disabled until somebody runs
``mcp_registry_reload`` (a ~6s heavy operation that purges
``sys.modules`` and re-imports everything).

This module closes that papercut. The dispatch path (gateway + conductor)
calls into this module on hitting a disabled capability or unavailable
tool; we re-probe the missing tool(s), and if they're now available we
restore the capability to the live registry without rebuilding.

Public API
----------

- :func:`recheck_tool` — re-probe a single tool, return whether it's now
  available. Used by the conductor's per-step gating to recover
  stale-unavailable tools.

- :func:`recheck_disabled_capability` — re-probe the missing tools for a
  named disabled capability and, if all are now available, restore the
  capability to the live registry. Used by the gateway's ``wb_run``
  dispatch path before returning the disabled error.

Both honour a per-tool cool-down (default 30s) to avoid hammering on a
genuinely-down tool. Both serialize through a single module-level
``RLock`` so concurrent agent calls don't double-probe.

Concurrency policy
------------------

The single ``_RECOVERY_LOCK`` (RLock) serializes:

- every entry into :func:`recheck_disabled_capability` and :func:`recheck_tool`;
- the mutation phase of ``mcp_registry_reload`` (acquired inside
  :func:`reload_registry_under_lock`, the lock-aware wrapper for
  ``invalidate_registry``).

Cool-down is checked INSIDE the lock so the second concurrent caller
serializes, sees the just-finished probe, and reuses the result without
re-probing.

Order of mutations on restore: assign to ``_REGISTRY`` BEFORE popping
from ``_DISABLED_REGISTRY`` and ``DISABLED_CAPABILITIES``, so concurrent
readers always see the capability in at least one of the maps.
"""

from __future__ import annotations

import threading
import time

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# Module-level state — protected by _RECOVERY_LOCK.
_RECOVERY_LOCK = threading.RLock()
_LAST_RECHECK_AT: dict[str, float] = {}

# Cool-down: skip a re-probe if the last one for this tool finished less
# than this many seconds ago. Prevents tight-loop hammering when a tool
# is genuinely down. Per-tool, monotonic clock.
_COOLDOWN_SECONDS: float = 30.0


def get_cooldown_seconds() -> float:
    """Return the current cool-down threshold in seconds.

    Default 30s. Can be overridden at module-import time via the
    ``WB_RECHECK_COOLDOWN_SECS`` env var (parsed once at import) or
    set programmatically via :func:`set_cooldown_seconds` (mostly for
    tests).
    """
    return _COOLDOWN_SECONDS


def set_cooldown_seconds(value: float) -> None:
    """Override the cool-down threshold. Test hook + emergency knob."""
    global _COOLDOWN_SECONDS
    _COOLDOWN_SECONDS = max(0.0, float(value))


# Read env override once at import.
import os as _os
try:
    _env_override = _os.environ.get("WB_RECHECK_COOLDOWN_SECS")
    if _env_override is not None:
        _COOLDOWN_SECONDS = max(0.0, float(_env_override))
except (TypeError, ValueError):
    pass
del _os


def recheck_tool(tool_id: str, *, force: bool = False) -> bool:
    """Re-probe a single tool, returning whether it's now available.

    Honours the per-tool cool-down: if the last re-probe for this tool
    finished less than :func:`get_cooldown_seconds` ago AND ``force`` is
    False, skip the probe and return the current cached availability.

    Args:
        tool_id: The probe ID (e.g. ``"obsidian"``, ``"chrome_extension"``).
        force: Bypass the cool-down. Used by ``mcp_registry_reload`` and
            similar deliberate refreshes.

    Returns:
        Current ``is_tool_available(tool_id)`` after any re-probe. If
        ``tool_id`` isn't a registered probe, returns whatever
        ``is_tool_available`` says (typically False).
    """
    from work_buddy.tools import is_tool_available, reprobe_one

    with _RECOVERY_LOCK:
        last = _LAST_RECHECK_AT.get(tool_id, 0.0)
        elapsed = time.monotonic() - last
        if not force and elapsed < _COOLDOWN_SECONDS:
            current = is_tool_available(tool_id)
            logger.debug(
                "recheck_tool(%s): cool-down hit (%.1fs < %.1fs), "
                "returning cached availability=%s",
                tool_id, elapsed, _COOLDOWN_SECONDS, current,
            )
            return current

        logger.info("recheck_tool(%s): probing (force=%s)", tool_id, force)
        try:
            reprobe_one(tool_id)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "recheck_tool(%s): reprobe_one raised: %s", tool_id, exc,
            )
            # Don't update _LAST_RECHECK_AT — let the next call retry.
            return is_tool_available(tool_id)

        _LAST_RECHECK_AT[tool_id] = time.monotonic()
        result = is_tool_available(tool_id)
        logger.info(
            "recheck_tool(%s): probe complete, available=%s",
            tool_id, result,
        )
        return result


def recheck_disabled_capability(name: str, *, force: bool = False) -> bool:
    """Try to recover a capability from ``DISABLED_CAPABILITIES``.

    Re-probes each of the capability's missing tools (honouring per-tool
    cool-down unless ``force=True``). If ALL missing tools are now
    available, restores the capability to the live registry by moving
    it from ``_DISABLED_REGISTRY`` to ``_REGISTRY`` and clearing it
    from ``DISABLED_CAPABILITIES``.

    Args:
        name: The capability name. Must be a key in ``DISABLED_CAPABILITIES``.
        force: Bypass per-tool cool-downs (mainly for tests).

    Returns:
        ``True`` if the capability is now in the live registry (either
        because it was already there — concurrent caller restored it —
        or because this call restored it).
        ``False`` if the capability remains disabled. In this case
        ``DISABLED_CAPABILITIES[name]`` is updated to reflect the
        current set of still-missing tools (which may be smaller than
        before if SOME tools recovered).

    If ``name`` is not in ``DISABLED_CAPABILITIES``, returns ``True``
    without probing — the capability is either already live or genuinely
    unknown (the caller must distinguish those two cases by checking
    ``_REGISTRY`` separately).
    """
    from work_buddy.tools import DISABLED_CAPABILITIES, is_tool_available
    from work_buddy.mcp_server.registry import (
        _DISABLED_REGISTRY,
        get_registry,
    )

    with _RECOVERY_LOCK:
        # Early return: capability is no longer disabled. Either it was
        # never there (caller's check is stale) or a concurrent caller
        # restored it while we were waiting on the lock.
        missing = DISABLED_CAPABILITIES.get(name)
        if missing is None:
            logger.debug(
                "recheck_disabled_capability(%s): not in DISABLED_CAPABILITIES, "
                "returning True (already restored or never disabled)",
                name,
            )
            return True

        logger.info(
            "recheck_disabled_capability(%s): missing=%s, force=%s",
            name, missing, force,
        )

        # Re-probe each missing tool. We don't recurse into recheck_tool
        # because we already hold _RECOVERY_LOCK (RLock makes that safe
        # but deepens the call stack; inline the check + probe).
        from work_buddy.tools import reprobe_one

        for tool_id in list(missing):
            last = _LAST_RECHECK_AT.get(tool_id, 0.0)
            elapsed = time.monotonic() - last
            if not force and elapsed < _COOLDOWN_SECONDS:
                logger.debug(
                    "recheck_disabled_capability(%s): tool %s cool-down "
                    "(%.1fs < %.1fs), skipping probe",
                    name, tool_id, elapsed, _COOLDOWN_SECONDS,
                )
                continue
            try:
                reprobe_one(tool_id)
                _LAST_RECHECK_AT[tool_id] = time.monotonic()
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "recheck_disabled_capability(%s): reprobe_one(%s) "
                    "raised: %s",
                    name, tool_id, exc,
                )

        # Re-evaluate which tools are still missing.
        still_missing = [t for t in missing if not is_tool_available(t)]

        if still_missing:
            # Update the disabled list — may have shrunk if some tools
            # recovered. Caller can render a more accurate error.
            DISABLED_CAPABILITIES[name] = still_missing
            logger.info(
                "recheck_disabled_capability(%s): still disabled, "
                "missing=%s (was %s)",
                name, still_missing, missing,
            )
            return False

        # All tools now available — restore the capability.
        # Trigger a registry build if it hasn't run yet (which would
        # also populate _DISABLED_REGISTRY). Then perform the swap.
        get_registry()  # ensures _REGISTRY is initialised

        capability = _DISABLED_REGISTRY.get(name)
        if capability is None:
            # Defensive: DISABLED_CAPABILITIES had the name but
            # _DISABLED_REGISTRY didn't. Should not happen post-CP-A1
            # invariants, but log loudly and treat as still-disabled.
            logger.error(
                "recheck_disabled_capability(%s): tools recovered but "
                "_DISABLED_REGISTRY has no Capability object — "
                "invariant violated. Forcing full registry rebuild.",
                name,
            )
            return False

        # Order matters: assign to _REGISTRY BEFORE popping from disabled
        # maps so concurrent readers always see the cap in at least one.
        from work_buddy.mcp_server.registry import _REGISTRY
        if _REGISTRY is None:
            # get_registry() above should have initialised this — but
            # belt-and-suspenders.
            logger.error(
                "recheck_disabled_capability(%s): _REGISTRY is None after "
                "get_registry() — aborting restore",
                name,
            )
            return False

        _REGISTRY[name] = capability
        _DISABLED_REGISTRY.pop(name, None)
        DISABLED_CAPABILITIES.pop(name, None)

        logger.info(
            "recheck_disabled_capability(%s): RESTORED to live registry",
            name,
        )
        return True


def reload_registry_under_lock() -> None:
    """Lock-aware wrapper for ``invalidate_registry``.

    The full-registry-reload path (``mcp_registry_reload``) needs to
    serialize against in-flight rechecks: a recheck holds the lock and
    might be mid-mutation when reload comes through. This wrapper
    acquires ``_RECOVERY_LOCK`` around the reload call so the two
    operations are mutually exclusive.

    Both orderings are safe:
      - reload-in-flight: rechecks queue, then see the freshly-rebuilt
        registry and operate on it.
      - recheck-in-flight: reload waits, then rebuilds (clearing
        _DISABLED_REGISTRY and _LAST_RECHECK_AT in the process — see
        :func:`_clear_recovery_state_on_reload`).
    """
    from work_buddy.mcp_server.registry import invalidate_registry

    with _RECOVERY_LOCK:
        # Clear cool-down timestamps so post-reload probes run fresh.
        # _DISABLED_REGISTRY clearing is already handled by
        # _build_registry() at the top of the filter pass (CP-A1).
        _LAST_RECHECK_AT.clear()
        invalidate_registry()
