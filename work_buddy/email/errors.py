"""Typed failure model for email providers.

Mirrors the Obsidian bridge's exception design — providers raise these from
their methods so capability wrappers can `isinstance`-classify rather than
substring-match error strings.
"""

from __future__ import annotations


class EmailError(Exception):
    """Base for all email-provider errors. ``error_kind`` is a stable string
    classifier consumers can key off without loading this module."""

    error_kind: str = "email_unknown"


class EmailProviderDisabled(EmailError):
    """The configured provider is disabled by user config or feature flag."""

    error_kind = "email_provider_disabled"


class EmailBridgeUnreachable(EmailError):
    """The backend bridge (e.g. Thunderbird) is not reachable.

    Terminal in the retry sense — retrying without the user opening
    Thunderbird doesn't help.
    """

    error_kind = "email_bridge_unreachable"


class EmailBridgeUnauthorized(EmailError):
    """The bridge rejected the auth token.

    Typically means the connection file is stale (Thunderbird restarted) or
    points at a different process. Recovery: re-discover the connection file.
    """

    error_kind = "email_bridge_unauthorized"


class EmailMessageNotFound(EmailError):
    """Looked-up message doesn't exist in the provider's index.

    May indicate the message was moved between collection and follow-up.
    """

    error_kind = "email_message_not_found"


class EmailProviderError(EmailError):
    """Generic provider-side failure — bridge responded with a 4xx/5xx that
    doesn't map onto a more specific kind."""

    error_kind = "email_provider_error"
