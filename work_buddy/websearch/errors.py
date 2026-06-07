"""Typed failure model for websearch providers.

Mirrors the email/calendar provider exception design — backends raise these
from their methods so the router and capability wrappers can
``isinstance``-classify rather than substring-match error strings. ``error_kind``
is a stable string consumers can key off without importing this module.

Routing semantics the router relies on (see :mod:`work_buddy.websearch.router`):
``WebSearchBadKey`` / ``WebSearchRateLimited`` / ``WebSearchUnavailable`` from a
backend mean "skip this backend, fall through to the next"; only when every
configured backend fails does the router raise ``WebSearchUnavailable`` to the
caller.
"""

from __future__ import annotations


class WebSearchError(Exception):
    """Base for all websearch errors. ``error_kind`` is a stable classifier."""

    error_kind: str = "websearch_unknown"


class WebSearchProviderDisabled(WebSearchError):
    """The subsystem is disabled by config, or an unknown provider was named.

    Mirrors the calendar/email convention of reusing the disabled error for
    "no usable provider here."
    """

    error_kind = "websearch_provider_disabled"


class WebSearchUnavailable(WebSearchError):
    """A backend is reachable-in-principle but returned nothing usable, or every
    configured backend failed. Retryable in the loose sense."""

    error_kind = "websearch_unavailable"


class WebSearchRateLimited(WebSearchError):
    """The backend rejected the call for rate/quota reasons (HTTP 402/429, ddgs
    ``RatelimitException``). The router falls through to the next backend."""

    error_kind = "websearch_rate_limited"


class WebSearchTimeout(WebSearchError):
    """The backend call exceeded its wall-clock budget (ddgs hang-guard or an
    httpx timeout)."""

    error_kind = "websearch_timeout"


class WebSearchBadKey(WebSearchError):
    """A required credential is missing or rejected (e.g. no ``JINA_API_KEY``).
    The router treats this as "skip this backend, fall through" — which is why
    a keyless install transparently uses ddgs."""

    error_kind = "websearch_bad_key"
