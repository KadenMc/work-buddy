"""Thin MCP-facing capability callables for the websearch subsystem.

Each mirrors the ``email/capabilities.py`` shape: instantiate nothing heavy,
do one round-trip through the router/extractor, and return a JSON-serialisable
``{ok, …, error_kind}`` dict (never raise across the gateway boundary). These
are registered as ops in ``work_buddy/mcp_server/ops/websearch_ops.py`` and
declared as ``kind: capability`` units under ``knowledge/store/websearch/``.

Storage policy: ``web_search`` is **ephemeral by default** — it does not cache
(the opt-in cache is for in-process reuse consumers like a watcher, via
``router.search(cache=True)``).
"""

from __future__ import annotations

import logging

from work_buddy.websearch.errors import WebSearchError

log = logging.getLogger(__name__)


def web_search(
    *,
    query: str,
    max_results: int = 8,
    topic: str | None = None,
    time_range: str | None = None,
) -> dict:
    """Run a routed web search (Jina → ddgs fallback). Returns
    ``{ok, count, provider, hits:[…]}`` or ``{ok:False, error, error_kind}``."""
    if not query or not str(query).strip():
        return {"ok": False, "error": "query is required", "error_kind": "bad_request"}
    from work_buddy.websearch import router
    try:
        hits = router.search(
            query, max_results=int(max_results), topic=topic, time_range=time_range,
        )
    except WebSearchError as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}
    return {
        "ok": True,
        "query": query,
        "count": len(hits),
        "provider": hits[0].provider if hits else None,
        "hits": [h.to_dict() for h in hits],
    }


def web_search_health() -> dict:
    """Report the active backend (first usable in the routing order) and its
    readiness. ddgs is keyless so it is always usable; Jina needs a key."""
    from work_buddy.websearch.provider import get_search_provider
    from work_buddy.websearch.router import active_backend
    try:
        name = active_backend()
        if not name:
            return {"ok": False, "error": "no usable websearch backend",
                    "error_kind": "websearch_unavailable"}
        provider = get_search_provider(name)
        return {"ok": True, "active_backend": name, **provider.health()}
    except WebSearchError as exc:
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


def web_fetch(*, url: str) -> dict:
    """Fetch + extract clean text for ``url`` (Jina reader when keyed, else
    trafilatura). Returns ``{ok, url, chars, extractor, text}``. Extraction is
    best-effort: an unreachable page returns ``ok:True`` with empty text and
    ``extractor:"none"`` rather than an error."""
    if not url or not str(url).strip():
        return {"ok": False, "error": "url is required", "error_kind": "bad_request"}
    from work_buddy.websearch.extract import extract_text
    try:
        fr = extract_text(url)
    except WebSearchError as exc:  # extract is best-effort, but stay defensive
        return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}
    return {
        "ok": True,
        "url": fr.url,
        "canonical_url": fr.canonical_url,
        "chars": len(fr.text),
        "extractor": fr.extractor,
        "fetched_at": fr.fetched_at,
        "text": fr.text,
    }
