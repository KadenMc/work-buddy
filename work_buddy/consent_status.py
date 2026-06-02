"""Read-only consent-request status, fused for shell-level pollers.

A consent request's lifecycle state lives in the shared notification store
(:func:`work_buddy.consent.get_consent_request`), while the *grant* it
produces lands in a session-scoped SQLite DB
(:func:`work_buddy.consent.list_consents`). Neither alone answers the
question a shell watcher actually asks — *"can I retry yet?"* — so this
module fuses them into a single ``pending | granted | denied | expired |
not_found`` verdict.

Design constraints:

* **Strictly read-only.** This never mints, caches, or consumes a grant.
  A ``granted`` verdict only tells the caller the user approved; the actual
  retry goes back through the gateway, whose ``@requires_consent`` gate
  re-checks the grant against the live principal. If the grant has since
  expired, the gate re-prompts. This preserves the invariant that grants
  do not time-travel through the retry queue.
* **Session-scoped grant reads.** The grant cross-check passes
  ``agent_session_id`` explicitly so it resolves against the agent's own
  ``consent.db`` rather than whatever session a shared cache last touched.
* **Light imports.** ``work_buddy.consent`` is imported lazily inside the
  functions so importing this module stays cheap for the CLI.

The decision signal (request status + recorded response) is authoritative
for *granted vs denied*. The grant cross-check is a best-effort second
opinion that catches the race where an out-of-band approval has written
the grant but the request record has not yet flipped to ``responded``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Verdicts
PENDING = "pending"
GRANTED = "granted"
DENIED = "denied"
EXPIRED = "expired"
NOT_FOUND = "not_found"


def consent_status(request_id: str, *, session_id: str | None = None) -> dict[str, Any]:
    """Return a normalized, read-only status view of a consent request.

    Parameters
    ----------
    request_id:
        The consent request id (the ``request_id`` the gateway hands back
        in a timeout result).
    session_id:
        The agent session whose ``consent.db`` to cross-check for the
        grant. When ``None``, the grant cross-check is skipped and the
        verdict rests on the request record alone (still correct for the
        common responded/denied/expired cases).

    Output shape (always includes ``state``)::

        {
          "request_id": str,
          "state": "pending"|"granted"|"denied"|"expired"|"not_found",
          "terminal": bool,            # True for granted/denied/expired
          "operation": str | None,     # the gated operation, if discoverable
          "response": str | None,      # the chosen option key, if responded
          "responded_at": str | None,
          "expires_at": str | None,
          "grant_seen": bool,          # the operation has a live grant
        }
    """
    from work_buddy.consent import get_consent_request

    req = get_consent_request(request_id)
    if req is None:
        return _view(request_id, NOT_FOUND, operation=None)

    operation = _extract_operation(req)
    raw_status = req.get("status")
    response_value = _response_value(req.get("response"))
    responded_at = req.get("responded_at")
    expires_at = req.get("expires_at")

    # Best-effort grant cross-check (catches out-of-band approvals).
    grant_seen = _grant_is_live(operation, session_id) if operation else False

    # 1. Explicit terminal request states.
    if raw_status in ("expired", "cancelled"):
        # An out-of-band grant landing after the request expired still
        # means the user approved — honour it.
        state = GRANTED if grant_seen else EXPIRED
        return _view(request_id, state, operation, response_value,
                     responded_at, expires_at, grant_seen)

    # 2. Responded → the user's recorded decision is authoritative.
    if raw_status == "responded":
        state = DENIED if response_value == "deny" else GRANTED
        return _view(request_id, state, operation, response_value,
                     responded_at, expires_at, grant_seen)

    # 3. Not yet responded, but a grant is already visible (race / out-of-band).
    if grant_seen:
        return _view(request_id, GRANTED, operation, response_value,
                     responded_at, expires_at, grant_seen)

    # 4. Still pending — but the request record may be past its TTL without
    #    having been swept yet (get_consent_request does not lazily expire).
    if _is_past(expires_at):
        return _view(request_id, EXPIRED, operation, response_value,
                     responded_at, expires_at, grant_seen)

    return _view(request_id, PENDING, operation, response_value,
                 responded_at, expires_at, grant_seen)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TERMINAL = {GRANTED, DENIED, EXPIRED}


def _view(
    request_id: str,
    state: str,
    operation: str | None,
    response: str | None = None,
    responded_at: str | None = None,
    expires_at: str | None = None,
    grant_seen: bool = False,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "state": state,
        "terminal": state in _TERMINAL,
        "operation": operation,
        "response": response,
        "responded_at": responded_at,
        "expires_at": expires_at,
        "grant_seen": grant_seen,
    }


def _extract_operation(req: dict[str, Any]) -> str | None:
    """Pull the gated operation from the consent request record.

    Prefers the structured ``custom_template.consent_meta.operation``;
    falls back to the ``op:<operation>`` tag that
    ``create_consent_request`` always attaches.
    """
    meta = (req.get("custom_template") or {}).get("consent_meta") or {}
    op = meta.get("operation")
    if op:
        return op
    for tag in req.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("op:"):
            return tag[len("op:"):]
    return None


def _response_value(response: Any) -> str | None:
    """Extract the chosen option key from a recorded response.

    Handles the StandardResponse dict shape ``{"value": ...}`` and the
    dashboard's nested ``{"value": {"value": "once"}}`` wrapping, mirroring
    the gateway's own unwrapping.
    """
    if response is None:
        return None
    value: Any = response
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    return value if isinstance(value, str) else None


def _grant_is_live(operation: str, session_id: str | None) -> bool:
    """True when ``operation`` has a non-expired grant in the session DB.

    Best-effort and defensive: any error reading the consent DB yields
    ``False`` rather than propagating, so a status query never fails on a
    grant-store hiccup.
    """
    if not session_id:
        return False
    try:
        from work_buddy.consent import list_consents

        grants = list_consents(agent_session_id=session_id)
        entry = grants.get(operation)
        if not entry:
            return False
        return not entry.get("expired", False)
    except Exception:
        return False


def _is_past(iso_ts: str | None) -> bool:
    """True when an ISO timestamp is in the past (UTC-aware)."""
    if not iso_ts:
        return False
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= ts
