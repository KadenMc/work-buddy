"""Quarantine a single pool entry by ``(run_id, item_id)``.

Lighter-weight counterpart to :func:`triage_pool_sweep`: when the
caller already knows a specific entry is stale (e.g. an "open in app"
action click came back with ``email_message_not_found``), there's no
need to re-probe every pending entry. Just flag the one we know is
gone.

Used by the dashboard's action-click error handler to self-heal stale
cards without waiting for the cron sweep. Equivalent to what
:func:`trigger_source_removed` would conclude on the next sweep —
short-circuits to "yes, it's gone, mark it" because the user has
already produced the evidence (the bridge said the message is no
longer findable).

Idempotent: if the entry is already in another state (already
quarantined, marked stale, reviewed), :meth:`TriagePool.mark_state`
no-ops. Returns the count of state changes (0 or 1).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def triage_pool_quarantine_entry(
    *,
    run_id: str,
    item_id: str,
    reason: str = "source_removed",
) -> dict[str, Any]:
    """Quarantine one pool entry.

    Args:
        run_id: Pool run id (visible on the Review-card group as
            ``pool_run_id``). Required.
        item_id: TriageItem id (visible on the Review-card item as
            ``id``). Required.
        reason: Stable string describing why. Defaults to
            ``"source_removed"`` — the same reason
            :func:`trigger_source_removed` emits, so daily sweep and
            on-click quarantines look identical in the audit trail.

    Returns:
        ``{"ok": True, "stamped": int}`` on success (stamped = 1 when
        the state changed, 0 when the entry was already in a terminal
        state). Validation errors return
        ``{"ok": False, "error": ..., "error_kind": ...}``.
    """
    if not run_id or not item_id:
        return {
            "ok": False,
            "error": "run_id and item_id are required",
            "error_kind": "bad_request",
        }
    if not isinstance(reason, str) or not reason.strip():
        return {
            "ok": False,
            "error": "reason must be a non-empty string",
            "error_kind": "bad_request",
        }

    try:
        from work_buddy.triage.background import get_pool
        pool = get_pool()
        stamped = pool.quarantine([(run_id, item_id)], reason=reason)
    except Exception as exc:  # noqa: BLE001 — surface any pool error to the caller
        logger.warning(
            "triage_pool_quarantine_entry: pool.quarantine raised for "
            "(run_id=%s, item_id=%s): %s",
            run_id, item_id, exc,
        )
        return {
            "ok": False,
            "error": f"pool.quarantine failed: {exc}",
            "error_kind": "pool_error",
        }

    return {
        "ok": True,
        "stamped": int(stamped),
        "run_id": run_id,
        "item_id": item_id,
        "reason": reason,
    }
