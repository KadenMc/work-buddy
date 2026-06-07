"""The system layer — routing, fallback, cache, across backends.

This is what makes websearch a *system* rather than two tools: every consumer
(the ``web_search`` capability, a future Events Processor, an agent) gets
backend routing, transparent fallback, and an opt-in cache for free.

``search()`` resolves ``websearch.routing`` (default ``[jina, ddgs]``) in order:
the first backend that returns a non-empty result set wins. A backend that
raises (missing key, rate limit, timeout, transport error) or returns nothing
is skipped and the next is tried. Only if *every* configured backend fails does
``search()`` raise :class:`WebSearchUnavailable`. This is why a keyless install
transparently serves from ddgs: Jina raises ``WebSearchBadKey``, the router logs
it and falls through.

Caching is **opt-in and off by default** (``cache=False``). When enabled, the
structured winning hits are stored under the normalized query (see
:mod:`work_buddy.websearch.cache`); raw page text is never persisted.
"""

from __future__ import annotations

import logging

from work_buddy.websearch import cache as _cache
from work_buddy.websearch.errors import (
    WebSearchError,
    WebSearchProviderDisabled,
    WebSearchUnavailable,
)
from work_buddy.websearch.models import SearchHit
from work_buddy.websearch.provider import get_search_provider

log = logging.getLogger(__name__)

_DEFAULT_ROUTING = ["jina", "ddgs"]


def _config() -> dict:
    from work_buddy.config import load_config
    return (load_config() or {}).get("websearch", {}) or {}


def search(
    query: str,
    *,
    max_results: int = 8,
    topic: str | None = None,
    time_range: str | None = None,
    since: str | None = None,
    routing: list[str] | None = None,
    cache: bool = False,
    ttl_hours: int | None = None,
) -> list[SearchHit]:
    """Routed, fallback-aware web search. Returns the first backend's non-empty
    hits. Raises :class:`WebSearchProviderDisabled` if the subsystem is disabled,
    or :class:`WebSearchUnavailable` if every configured backend fails."""
    if not query or not query.strip():
        return []

    cfg = _config()
    if cfg.get("enabled", True) is False:
        raise WebSearchProviderDisabled("websearch.enabled is False in config")

    order = routing or cfg.get("routing") or _DEFAULT_ROUTING

    if cache:
        cached = _cache.get(query, max_results=max_results, time_range=time_range)
        if cached is not None:
            log.debug("websearch cache hit for %r (%d hits)", query, len(cached))
            return cached

    failures: list[str] = []
    any_clean_response = False  # a backend that responded without erroring (even if empty)
    for name in order:
        try:
            provider = get_search_provider(name)
        except WebSearchError as exc:
            failures.append(f"{name}:{exc.error_kind}")
            log.info("websearch: skip backend %r (%s)", name, exc.error_kind)
            continue
        try:
            hits = provider.search(
                query, max_results=max_results, topic=topic,
                time_range=time_range, since=since,
            )
        except WebSearchError as exc:
            failures.append(f"{name}:{exc.error_kind}")
            log.info("websearch: backend %r failed (%s), falling through", name, exc.error_kind)
            continue

        any_clean_response = True
        if hits:
            log.info("websearch: served by %r (%d hits)", name, len(hits))
            if cache:
                ttl = ttl_hours if ttl_hours is not None else (cfg.get("cache", {}) or {}).get("ttl_hours")
                _cache.put(
                    query, hits, provider=name,
                    max_results=max_results, time_range=time_range, ttl_hours=ttl,
                )
            return hits
        log.debug("websearch: backend %r returned 0 hits, falling through", name)
        failures.append(f"{name}:empty")

    # At least one backend responded cleanly but nothing matched → legitimate
    # empty result, not a failure. Only raise when every backend errored.
    if any_clean_response:
        log.info("websearch: no results for %r (clean empty from %s)", query, order)
        return []

    raise WebSearchUnavailable(
        f"all websearch backends failed for {query!r} "
        f"(tried {order}: {'; '.join(failures)})"
    )


def active_backend(routing: list[str] | None = None) -> str | None:
    """Best-effort: which backend would serve right now (first usable in the
    routing order). Used by the health probe. Returns ``None`` if none usable."""
    cfg = _config()
    if cfg.get("enabled", True) is False:
        return None
    order = routing or cfg.get("routing") or _DEFAULT_ROUTING
    for name in order:
        try:
            provider = get_search_provider(name)
        except WebSearchError:
            continue
        try:
            if provider.health().get("ok"):
                return name
        except Exception:  # noqa: BLE001 — health must never raise to the probe
            continue
    return None
