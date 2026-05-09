"""Capability callables for the email/Thunderbird integration.

Registered in :mod:`work_buddy.mcp_server.registry` (see
``_email_capabilities()``). All callables are lightweight: they instantiate
the configured provider on demand, perform one HTTP round-trip, and return
JSON-serialisable dicts. No heavy imports — keeps the gateway snappy.

Surface:
  - ``email_health``     Probe-style status, returns the bridge's /health.
  - ``email_accounts``   List accounts visible through the bridge.
  - ``email_get``        Fetch one message by stable handle.
  - ``email_display``    Open a message in the user's mail UI.

Triage
------

Email triage runs through the unified source pipeline at
``work_buddy.pipelines.email.EmailTriagePipeline`` (dispatched via the
``run_source_pipeline`` capability with ``source='email_triage'``); the
legacy ``email_triage_run`` capability that used to live in this module
was retired during the clarify -> Threads migration.
"""

from __future__ import annotations

import logging
from typing import Any

from work_buddy.email.errors import EmailError, EmailMessageNotFound
from work_buddy.email.models import EmailMessageHandle
from work_buddy.email.provider import get_email_provider

log = logging.getLogger(__name__)


def _provider_or_error() -> tuple[Any, dict | None]:
    try:
        return get_email_provider(), None
    except EmailError as exc:
        return None, {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


# ---------------------------------------------------------------------------
# Read-only diagnostics
# ---------------------------------------------------------------------------


def email_health() -> dict:
    """Liveness probe — return the configured provider's health payload."""
    provider, err = _provider_or_error()
    if err:
        return err
    try:
        return {"ok": True, "provider": provider.name, **provider.health()}
    except EmailError as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


def email_accounts() -> dict:
    """List the accounts the bridge currently exposes."""
    provider, err = _provider_or_error()
    if err:
        return err
    try:
        accounts = provider.list_accounts()
        allowed = [a for a in accounts if a.get("allowed", False)]
        return {
            "ok": True,
            "provider": provider.name,
            "accounts": accounts,
            "allowed_count": len(allowed),
        }
    except EmailError as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


# ---------------------------------------------------------------------------
# Single-message follow-ups
# ---------------------------------------------------------------------------


def email_get(
    *,
    provider_message_id: str,
    folder_path: str,
    max_body_chars: int = 8000,
) -> dict:
    """Fetch one message including body. Operates on the operational handle
    (provider_message_id + folder_path), not the stable key."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not provider_message_id or not folder_path:
        return {"ok": False, "error": "provider_message_id and folder_path are required",
                "error_kind": "bad_request"}
    handle = EmailMessageHandle(
        provider_message_id=provider_message_id, folder_path=folder_path,
    )
    try:
        msg = provider.get_message(handle, max_body_chars=max_body_chars)
        return {"ok": True, "provider": provider.name, **msg.to_dict()}
    except EmailMessageNotFound as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}
    except EmailError as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


def email_display(
    *,
    provider_message_id: str,
    folder_path: str,
    mode: str = "3pane",
) -> dict:
    """Open a message in Thunderbird's UI. ``mode`` is one of
    ``3pane`` (focus the message in the main folder pane), ``tab``, or
    ``window``."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not provider_message_id or not folder_path:
        return {"ok": False, "error": "provider_message_id and folder_path are required",
                "error_kind": "bad_request"}
    handle = EmailMessageHandle(
        provider_message_id=provider_message_id, folder_path=folder_path,
    )
    try:
        return {"ok": True, "provider": provider.name,
                **provider.display_message(handle, mode=mode)}
    except EmailMessageNotFound as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}
    except EmailError as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}
