"""Consent-domain ops.

Each op here is referenced by a capability declaration (a ``kind: "capability"``
knowledge-store unit carrying a matching ``op`` field).
"""

from __future__ import annotations

from work_buddy.mcp_server.op_registry import register_op


def _register() -> None:
    from work_buddy import consent

    register_op("op.wb.consent_list", consent.list_consents)
    register_op("op.wb.consent_request_list", consent.list_pending_requests)


_register()
