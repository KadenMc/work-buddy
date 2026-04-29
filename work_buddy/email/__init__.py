"""work-buddy email integration — provider abstraction + Thunderbird backend.

Public surface
--------------

- :class:`EmailProvider` — abstract protocol implemented per backend.
- :class:`ThunderbirdEmailProvider` — HTTP client for thunderbird-work-buddy.
- :class:`FakeEmailProvider` — in-memory provider used by tests and dry runs.
- :class:`EmailSummary`, :class:`EmailMessage` — provider-agnostic shapes.
- :class:`EmailError` and subclasses — typed failure model.
- :func:`get_email_provider` — config-driven factory.

The triage glue lives in :mod:`work_buddy.email.triage_adapter`; the MCP
capabilities and `ToolProbe` registration live in
:mod:`work_buddy.mcp_server.registry` and :mod:`work_buddy.tools` respectively.
"""

from work_buddy.email.errors import (
    EmailBridgeUnreachable,
    EmailBridgeUnauthorized,
    EmailError,
    EmailMessageNotFound,
    EmailProviderDisabled,
    EmailProviderError,
)
from work_buddy.email.models import (
    EmailFolder,
    EmailMessage,
    EmailMessageHandle,
    EmailSummary,
    stable_key_for,
)
from work_buddy.email.provider import EmailProvider, get_email_provider

__all__ = [
    "EmailBridgeUnauthorized",
    "EmailBridgeUnreachable",
    "EmailError",
    "EmailFolder",
    "EmailMessage",
    "EmailMessageHandle",
    "EmailMessageNotFound",
    "EmailProvider",
    "EmailProviderDisabled",
    "EmailProviderError",
    "EmailSummary",
    "get_email_provider",
    "stable_key_for",
]
