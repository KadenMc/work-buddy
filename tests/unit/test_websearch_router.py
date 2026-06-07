"""Unit tests — router (routing/fallback) + opt-in cache. No network: stub
providers are injected by patching router.get_search_provider; the cache file
is redirected to a tmp path.
"""

from __future__ import annotations

import pytest

import work_buddy.config as cfgmod
import work_buddy.websearch.cache as cache_mod
import work_buddy.websearch.router as router
from work_buddy.websearch.errors import (
    WebSearchBadKey,
    WebSearchProviderDisabled,
    WebSearchRateLimited,
    WebSearchUnavailable,
)
from work_buddy.websearch.models import SearchHit


class _Stub:
    def __init__(self, name, hits=None, exc=None):
        self.name = name
        self._hits = hits or []
        self._exc = exc
        self.calls = 0

    def search(self, query, **kw):
        self.calls += 1
        if self._exc:
            raise self._exc
        return list(self._hits)

    def health(self):
        return {"ok": True}


def _hit(name):
    return SearchHit(title=f"{name}-t", url=f"https://{name}", snippet="s", provider=name)


def _wire(monkeypatch, providers, cfg=None):
    monkeypatch.setattr(router, "get_search_provider", lambda name: providers[name])
    monkeypatch.setattr(cfgmod, "load_config",
                        lambda *a, **k: {"websearch": cfg or {"routing": ["jina", "ddgs"]}})


# ---------------------------------------------------------------------------
# Routing + fallback
# ---------------------------------------------------------------------------


def test_first_backend_wins(monkeypatch):
    jina = _Stub("jina", hits=[_hit("jina")])
    ddgs = _Stub("ddgs", hits=[_hit("ddgs")])
    _wire(monkeypatch, {"jina": jina, "ddgs": ddgs})
    hits = router.search("q")
    assert hits[0].provider == "jina"
    assert ddgs.calls == 0  # never reached


def test_falls_through_on_bad_key(monkeypatch):
    jina = _Stub("jina", exc=WebSearchBadKey("no key"))
    ddgs = _Stub("ddgs", hits=[_hit("ddgs")])
    _wire(monkeypatch, {"jina": jina, "ddgs": ddgs})
    hits = router.search("q")
    assert hits[0].provider == "ddgs"
    assert jina.calls == 1 and ddgs.calls == 1


def test_falls_through_on_empty(monkeypatch):
    jina = _Stub("jina", hits=[])  # empty → fall through
    ddgs = _Stub("ddgs", hits=[_hit("ddgs")])
    _wire(monkeypatch, {"jina": jina, "ddgs": ddgs})
    assert router.search("q")[0].provider == "ddgs"


def test_falls_through_on_rate_limit(monkeypatch):
    jina = _Stub("jina", exc=WebSearchRateLimited("429"))
    ddgs = _Stub("ddgs", hits=[_hit("ddgs")])
    _wire(monkeypatch, {"jina": jina, "ddgs": ddgs})
    assert router.search("q")[0].provider == "ddgs"


def test_all_fail_raises_unavailable(monkeypatch):
    jina = _Stub("jina", exc=WebSearchBadKey("no key"))
    ddgs = _Stub("ddgs", exc=WebSearchRateLimited("429"))
    _wire(monkeypatch, {"jina": jina, "ddgs": ddgs})
    with pytest.raises(WebSearchUnavailable):
        router.search("q")


def test_all_clean_empty_returns_empty_list(monkeypatch):
    # Backends respond without error but find nothing → legitimate empty, NOT a raise.
    jina = _Stub("jina", hits=[])
    ddgs = _Stub("ddgs", hits=[])
    _wire(monkeypatch, {"jina": jina, "ddgs": ddgs})
    assert router.search("q") == []


def test_mixed_error_then_clean_empty_returns_empty(monkeypatch):
    jina = _Stub("jina", exc=WebSearchBadKey("no key"))  # errors
    ddgs = _Stub("ddgs", hits=[])                          # clean empty
    _wire(monkeypatch, {"jina": jina, "ddgs": ddgs})
    assert router.search("q") == []


def test_disabled_raises(monkeypatch):
    _wire(monkeypatch, {}, cfg={"enabled": False, "routing": ["ddgs"]})
    with pytest.raises(WebSearchProviderDisabled):
        router.search("q")


def test_empty_query_returns_empty(monkeypatch):
    _wire(monkeypatch, {"ddgs": _Stub("ddgs", hits=[_hit("ddgs")])}, cfg={"routing": ["ddgs"]})
    assert router.search("   ") == []


def test_unknown_backend_in_routing_is_skipped(monkeypatch):
    # factory raises Disabled for unknown name → router skips, tries next
    def factory(name):
        if name == "bogus":
            raise WebSearchProviderDisabled("unknown")
        return _Stub("ddgs", hits=[_hit("ddgs")])
    monkeypatch.setattr(router, "get_search_provider", factory)
    monkeypatch.setattr(cfgmod, "load_config",
                        lambda *a, **k: {"websearch": {"routing": ["bogus", "ddgs"]}})
    assert router.search("q")[0].provider == "ddgs"


# ---------------------------------------------------------------------------
# Cache (opt-in)
# ---------------------------------------------------------------------------


def test_cache_hit_avoids_second_backend_call(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", tmp_path / "wc.json")
    ddgs = _Stub("ddgs", hits=[_hit("ddgs")])
    _wire(monkeypatch, {"ddgs": ddgs}, cfg={"routing": ["ddgs"], "cache": {"ttl_hours": 12}})

    first = router.search("same query", cache=True)
    second = router.search("same query", cache=True)
    assert [h.url for h in first] == [h.url for h in second]
    assert ddgs.calls == 1  # second served from cache


def test_cache_false_persists_nothing(monkeypatch, tmp_path):
    cache_file = tmp_path / "wc.json"
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", cache_file)
    ddgs = _Stub("ddgs", hits=[_hit("ddgs")])
    _wire(monkeypatch, {"ddgs": ddgs}, cfg={"routing": ["ddgs"]})

    router.search("q", cache=False)
    router.search("q", cache=False)
    assert ddgs.calls == 2  # no caching
    assert not cache_file.exists()  # nothing written


def test_cache_roundtrip_reconstructs_hits(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", tmp_path / "wc.json")
    ddgs = _Stub("ddgs", hits=[_hit("ddgs")])
    _wire(monkeypatch, {"ddgs": ddgs}, cfg={"routing": ["ddgs"]})
    router.search("roundtrip", cache=True)
    cached = cache_mod.get("roundtrip", max_results=8, time_range=None)
    assert cached and isinstance(cached[0], SearchHit) and cached[0].provider == "ddgs"
