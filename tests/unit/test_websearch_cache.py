"""Unit tests — websearch cache module: key normalization, put/get roundtrip,
expiry logic, and malformed-record handling. Redirects the cache file to a tmp
path; no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import work_buddy.websearch.cache as cache_mod
from work_buddy.websearch.models import SearchHit


@pytest.fixture
def tmp_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_mod, "_CACHE_PATH", tmp_path / "websearch_cache.json")
    return tmp_path


def _hit(i=0):
    return SearchHit(title=f"T{i}", url=f"https://x/{i}", snippet="s", provider="ddgs")


# ---------------------------------------------------------------------------
# Key normalization
# ---------------------------------------------------------------------------


def test_cache_key_normalizes_query():
    k1 = cache_mod.cache_key("  Hello   World ", max_results=8, time_range=None)
    k2 = cache_mod.cache_key("hello world", max_results=8, time_range=None)
    assert k1 == k2


def test_cache_key_varies_by_max_results_and_time_range():
    base = cache_mod.cache_key("q", max_results=8, time_range=None)
    assert base != cache_mod.cache_key("q", max_results=5, time_range=None)
    assert base != cache_mod.cache_key("q", max_results=8, time_range="w")


# ---------------------------------------------------------------------------
# put / get roundtrip
# ---------------------------------------------------------------------------


def test_put_get_roundtrip(tmp_cache):
    hits = [_hit(0), _hit(1)]
    cache_mod.put("query here", hits, provider="ddgs", max_results=8, time_range=None, ttl_hours=12)
    got = cache_mod.get("query here", max_results=8, time_range=None)
    assert got is not None
    assert [h.url for h in got] == [h.url for h in hits]
    assert all(isinstance(h, SearchHit) for h in got)


def test_get_miss_on_absent(tmp_cache):
    assert cache_mod.get("never stored", max_results=8, time_range=None) is None


def test_get_miss_on_different_params(tmp_cache):
    cache_mod.put("q", [_hit()], provider="ddgs", max_results=8, time_range=None)
    assert cache_mod.get("q", max_results=5, time_range=None) is None  # different max_results key


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_expired_helper():
    now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=1)).isoformat()
    assert cache_mod._expired({"expires_at": past}, now) is True
    assert cache_mod._expired({"expires_at": future}, now) is False
    assert cache_mod._expired({"expires_at": ""}, now) is False  # no expiry set → not expired
    assert cache_mod._expired({"expires_at": "not-a-date"}, now) is True  # malformed → treat expired


def test_get_returns_none_for_expired_entry(tmp_cache):
    # ttl_hours=0 → expires_at == put time; a get a moment later is past it.
    cache_mod.put("q", [_hit()], provider="ddgs", max_results=8, time_range=None, ttl_hours=0)
    assert cache_mod.get("q", max_results=8, time_range=None) is None


# ---------------------------------------------------------------------------
# Malformed record
# ---------------------------------------------------------------------------


def test_malformed_hits_record_returns_none(tmp_cache, monkeypatch):
    # Write a record whose hits don't match the SearchHit fields.
    import json
    key = cache_mod.cache_key("q", max_results=8, time_range=None)
    bad = {key: {
        "hits": [{"not_a_field": 1}],
        "provider": "ddgs",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }}
    cache_mod._CACHE_PATH.write_text(json.dumps(bad), encoding="utf-8")
    assert cache_mod.get("q", max_results=8, time_range=None) is None


def test_corrupt_cache_file_is_a_miss(tmp_cache):
    cache_mod._CACHE_PATH.write_text("{not valid json", encoding="utf-8")
    assert cache_mod.get("q", max_results=8, time_range=None) is None
