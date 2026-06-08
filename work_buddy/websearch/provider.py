"""Provider protocol + factory — one concrete backend per provider name.

Mirrors :func:`work_buddy.email.provider.get_email_provider` and
:func:`work_buddy.calendar.provider.get_calendar_provider` exactly: a
``@runtime_checkable`` Protocol, a factory driven by ``websearch.provider``
config with an ``enabled: false`` short-circuit, lazy adapter imports, a
``fake`` backend for tests, and typed errors so callers ``isinstance``-classify.

The *router* (:mod:`work_buddy.websearch.router`) is what turns these
single-backend providers into a system (routing, fallback, cache, quota);
this module only resolves one named backend.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from work_buddy.websearch.models import SearchHit


@runtime_checkable
class SearchProvider(Protocol):
    """Stable interface every search backend implements.

    Methods raise typed :class:`work_buddy.websearch.errors.WebSearchError`
    subclasses on failure. ``supports(feature)`` lets the router and extractor
    branch on backend capabilities (``"full_text"``, ``"time_filter"``,
    ``"news"``) without isinstance-checking concrete classes.
    """

    name: str
    """Short identifier for diagnostics, e.g. ``"jina"`` / ``"ddgs"``."""

    def health(self) -> dict:
        """Quick liveness/readiness payload for the health probe."""

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        topic: str | None = None,
        time_range: str | None = None,
        since: str | None = None,
    ) -> list[SearchHit]:
        """Return up to ``max_results`` hits for ``query``. Raises a
        ``WebSearchError`` subclass on failure."""

    def supports(self, feature: str) -> bool:
        """Whether the backend supports a capability: ``"full_text"`` (returns
        page text inline), ``"time_filter"``, ``"news"``."""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_search_provider(name: str) -> SearchProvider:
    """Return one concrete search backend by name.

    Selection mirrors the calendar/email factories: ``websearch.enabled: false``
    short-circuits with :class:`WebSearchProviderDisabled`; an unknown name
    raises the same (the calendar/email convention for "no usable provider").
    Concrete adapters are lazy-imported so the module stays cheap and a missing
    optional dependency only bites the backend that needs it.

    Tests override by importing :class:`FakeSearchProvider` directly.
    """
    from work_buddy.config import load_config
    from work_buddy.websearch.errors import WebSearchProviderDisabled

    cfg = (load_config() or {}).get("websearch", {}) or {}
    if cfg.get("enabled", True) is False:
        raise WebSearchProviderDisabled("websearch.enabled is False in config")

    n = (name or "").lower()
    if n == "jina":
        from work_buddy.websearch.providers.jina import JinaSearchProvider
        return JinaSearchProvider(cfg.get("jina", {}) or {})
    if n in ("ddgs", "duckduckgo"):
        from work_buddy.websearch.providers.ddgs_meta import DdgsSearchProvider
        return DdgsSearchProvider(cfg.get("ddgs", {}) or {})
    if n == "fake":
        from work_buddy.websearch.providers.fake import FakeSearchProvider
        return FakeSearchProvider()
    raise WebSearchProviderDisabled(f"Unknown websearch provider: {name!r}")
