"""Unit tests — thin capability callables + op registration. Router/extract are
patched; no network.
"""

from __future__ import annotations

import work_buddy.websearch.capabilities as caps
from work_buddy.websearch.errors import WebSearchUnavailable
from work_buddy.websearch.models import FetchResult, SearchHit


def test_web_search_ok(monkeypatch):
    import work_buddy.websearch.router as router
    monkeypatch.setattr(router, "search",
                        lambda q, **kw: [SearchHit(title="T", url="https://x", snippet="s", provider="ddgs")])
    out = caps.web_search(query="hello")
    assert out["ok"] is True and out["count"] == 1
    assert out["provider"] == "ddgs" and out["hits"][0]["url"] == "https://x"


def test_web_search_empty_is_ok_zero(monkeypatch):
    import work_buddy.websearch.router as router
    monkeypatch.setattr(router, "search", lambda q, **kw: [])
    out = caps.web_search(query="no results here")
    assert out["ok"] is True and out["count"] == 0 and out["provider"] is None


def test_web_search_requires_query():
    assert caps.web_search(query="")["error_kind"] == "bad_request"
    assert caps.web_search(query="   ")["error_kind"] == "bad_request"


def test_web_search_maps_error(monkeypatch):
    import work_buddy.websearch.router as router
    def boom(q, **kw):
        raise WebSearchUnavailable("all backends down")
    monkeypatch.setattr(router, "search", boom)
    out = caps.web_search(query="q")
    assert out["ok"] is False and out["error_kind"] == "websearch_unavailable"


def test_web_search_health(monkeypatch):
    import work_buddy.websearch.router as router
    import work_buddy.websearch.provider as provider_mod

    class _P:
        name = "ddgs"
        def health(self):
            return {"ok": True, "provider": "ddgs", "needs_key": False}

    monkeypatch.setattr(router, "active_backend", lambda routing=None: "ddgs")
    monkeypatch.setattr(provider_mod, "get_search_provider", lambda name: _P())
    out = caps.web_search_health()
    assert out["ok"] is True and out["active_backend"] == "ddgs"


def test_web_search_health_no_backend(monkeypatch):
    import work_buddy.websearch.router as router
    monkeypatch.setattr(router, "active_backend", lambda routing=None: None)
    out = caps.web_search_health()
    assert out["ok"] is False and out["error_kind"] == "websearch_unavailable"


def test_web_fetch(monkeypatch):
    import work_buddy.websearch.extract as extract_mod
    monkeypatch.setattr(extract_mod, "extract_text",
                        lambda url, **kw: FetchResult(url=url, canonical_url=url, text="body text",
                                                      fetched_at="2026-01-01T00:00:00Z", extractor="trafilatura"))
    out = caps.web_fetch(url="https://x")
    assert out["ok"] is True and out["chars"] == len("body text") and out["extractor"] == "trafilatura"


def test_web_fetch_requires_url():
    assert caps.web_fetch(url="")["error_kind"] == "bad_request"


def test_ops_registered():
    # Importing the ops module registers the ops; get_op resolves them.
    import work_buddy.mcp_server.ops.websearch_ops  # noqa: F401  (registers on import)
    from work_buddy.mcp_server.op_registry import get_op
    assert get_op("op.wb.web_search") is not None
    assert get_op("op.wb.web_search_health") is not None
    assert get_op("op.wb.web_fetch") is not None
