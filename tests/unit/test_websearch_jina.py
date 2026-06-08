"""Unit tests — Jina backend. No network: httpx.Client is wrapped with a
MockTransport. Live verification (one real s.jina.ai call) is done separately at
build time per the plan. Covers key handling, envelope parsing, full-text
mapping, status-code classification, and max_results slicing.
"""

from __future__ import annotations

import httpx
import pytest

import work_buddy.websearch.providers.jina as jina_mod
from work_buddy.websearch.errors import (
    WebSearchBadKey,
    WebSearchRateLimited,
    WebSearchUnavailable,
)
from work_buddy.websearch.providers.jina import JinaSearchProvider


def _mock_httpx(monkeypatch, handler):
    """Route the provider's httpx.Client through a MockTransport."""
    real_client = httpx.Client  # capture before patching to avoid recursion
    def factory(*a, **kw):
        kw.pop("transport", None)
        return real_client(*a, **kw, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(jina_mod.httpx, "Client", factory)


def _with_key(monkeypatch, key="test-key"):
    monkeypatch.setattr(jina_mod, "read_secret_env", lambda name: key)


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------


def test_missing_key_raises_badkey(monkeypatch):
    monkeypatch.setattr(jina_mod, "read_secret_env", lambda name: None)
    with pytest.raises(WebSearchBadKey):
        JinaSearchProvider({}).search("q")


def test_health_reflects_key_presence(monkeypatch):
    monkeypatch.setattr(jina_mod, "read_secret_env", lambda name: None)
    assert JinaSearchProvider({}).health()["ok"] is False
    monkeypatch.setattr(jina_mod, "read_secret_env", lambda name: "k")
    h = JinaSearchProvider({}).health()
    assert h["ok"] is True and h["needs_key"] is True


# ---------------------------------------------------------------------------
# Parsing + mapping
# ---------------------------------------------------------------------------


def test_search_maps_envelope_with_full_text(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["accept"] = request.headers.get("Accept")
        captured["q"] = request.url.params.get("q")
        return httpx.Response(200, json={"data": [
            {"title": "T", "url": "https://a", "description": "snip", "content": "FULL TEXT"},
        ]})

    _with_key(monkeypatch)
    _mock_httpx(monkeypatch, handler)
    hits = JinaSearchProvider({}).search("hello world", max_results=5)
    assert len(hits) == 1
    h = hits[0]
    assert h.title == "T" and h.url == "https://a" and h.snippet == "snip"
    assert h.raw_text == "FULL TEXT" and h.provider == "jina"
    # request shape
    assert captured["auth"] == "Bearer test-key"
    assert captured["accept"] == "application/json"
    assert captured["q"] == "hello world"


def test_search_handles_bare_list_payload(monkeypatch):
    def handler(request):
        return httpx.Response(200, json=[{"title": "X", "url": "https://x", "description": "d"}])
    _with_key(monkeypatch)
    _mock_httpx(monkeypatch, handler)
    hits = JinaSearchProvider({}).search("q")
    assert len(hits) == 1 and hits[0].title == "X"


def test_search_respects_max_results(monkeypatch):
    rows = [{"title": f"T{i}", "url": f"https://{i}", "description": "d"} for i in range(10)]
    def handler(request):
        return httpx.Response(200, json={"data": rows})
    _with_key(monkeypatch)
    _mock_httpx(monkeypatch, handler)
    assert len(JinaSearchProvider({}).search("q", max_results=3)) == 3


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code,exc", [
    (401, WebSearchBadKey),
    (403, WebSearchBadKey),
    (402, WebSearchRateLimited),
    (429, WebSearchRateLimited),
    (500, WebSearchUnavailable),
])
def test_status_codes_map_to_errors(monkeypatch, code, exc):
    def handler(request):
        return httpx.Response(code, json={"error": "x"})
    _with_key(monkeypatch)
    _mock_httpx(monkeypatch, handler)
    with pytest.raises(exc):
        JinaSearchProvider({}).search("q")


def test_non_json_body_raises_unavailable(monkeypatch):
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")
    _with_key(monkeypatch)
    _mock_httpx(monkeypatch, handler)
    with pytest.raises(WebSearchUnavailable):
        JinaSearchProvider({}).search("q")


def test_supports_full_text():
    assert JinaSearchProvider({}).supports("full_text") is True
    assert JinaSearchProvider({}).supports("news") is False
