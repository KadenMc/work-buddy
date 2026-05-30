"""Typed failure model for calendar providers.

Mirrors :mod:`work_buddy.email.errors` — providers raise these from their
methods so capability wrappers can ``isinstance``-classify rather than
substring-matching error strings. ``error_kind`` is a stable classifier
consumers can key off without importing this module.

Note: there is deliberately **no** ``CalendarWriteForbidden``. Per the design
(DECISIONS D2), write safety is *consent*, not a structural boundary — WB
writes to the user's real calendars under heavy per-change consent, so there
is no allowlist to forbid against. ``CalendarWriteUnsupported`` is different:
it marks a *provider that cannot write at all* (e.g. a read-only ICS feed),
which is a capability fact, not an authorization decision.
"""

from __future__ import annotations


class CalendarError(Exception):
    """Base for all calendar-provider errors."""

    error_kind: str = "calendar_unknown"


class CalendarProviderDisabled(CalendarError):
    """The configured provider is disabled by user config or feature flag."""

    error_kind = "calendar_provider_disabled"


class CalendarBridgeUnreachable(CalendarError):
    """The backend bridge (e.g. the Obsidian google-calendar plugin) is not
    reachable. Terminal in the retry sense — retrying without the user opening
    Obsidian / re-authenticating doesn't help."""

    error_kind = "calendar_bridge_unreachable"


class CalendarEventNotFound(CalendarError):
    """A looked-up event doesn't exist in the provider's index.

    May indicate the event was moved or deleted between collection and
    follow-up."""

    error_kind = "calendar_event_not_found"


class CalendarWriteUnsupported(CalendarError):
    """The configured provider cannot perform write operations.

    A capability fact (e.g. a read-only ICS adapter), not an authorization
    decision — see the module docstring."""

    error_kind = "calendar_write_unsupported"


class CalendarProviderError(CalendarError):
    """Generic provider-side failure — the backend responded with an error
    that doesn't map onto a more specific kind."""

    error_kind = "calendar_provider_error"
