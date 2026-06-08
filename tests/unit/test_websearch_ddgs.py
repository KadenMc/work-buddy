"""Unit tests — hardened ddgs backend. No network: a fake DDGS client is
injected via ``provider._ddgs``. Exercises result mapping, the wall-clock
timeout guard (stubbed hang), and rate-limit retry/backoff classification.
"""

from __future__ import annotations

import time

import pytest

from ddgs.exceptions import DDGSException, RatelimitException
from work_buddy.websearch.errors import (
    WebSearchRateLimited,
    WebSearchTimeout,
    WebSearchUnavailable,
)
from work_buddy.websearch.models import SearchHit
from work_buddy.websearch.providers.ddgs_meta import DdgsSearchProvider


class _FakeDDGS:
    def __init__(self, *, rows=None, raise_n_times=0, exc=RatelimitException, hang_s=0.0):
        self._rows = rows if rows is not None else []
        self._raise_n = raise_n_times
        self._exc = exc
        self._hang_s = hang_s
        self.calls = 0

    def text(self, query, **kwargs):
        self.calls += 1
        if self._hang_s:
            time.sleep(self._hang_s)
        if self.calls <= self._raise_n:
            raise self._exc("boom")
        return list(self._rows)


def _provider(fake, **over):
    p = DdgsSearchProvider({"min_interval_s": 0, "timeout_s": 1, "max_retries": 3, **over})
    p._ddgs = fake
    return p


def test_maps_rows_to_searchhits():
    fake = _FakeDDGS(rows=[
        {"title": "T1", "href": "https://a", "body": "snippet a"},
        {"title": "T2", "url": "https://b", "description": "snippet b"},  # variant keys
    ])
    hits = _provider(fake).search("q", max_results=5)
    assert all(isinstance(h, SearchHit) for h in hits)
    assert hits[0].title == "T1" and hits[0].url == "https://a" and hits[0].snippet == "snippet a"
    assert hits[1].url == "https://b" and hits[1].snippet == "snippet b"
    assert all(h.provider == "ddgs" for h in hits)


def test_empty_results_return_empty_list():
    assert _provider(_FakeDDGS(rows=[])).search("q") == []


def test_timeout_guard_fires_on_hang():
    # Worker sleeps 3s; timeout budget is 0.3s → WebSearchTimeout.
    fake = _FakeDDGS(rows=[{"title": "x", "href": "y", "body": "z"}], hang_s=3.0)
    p = _provider(fake, timeout_s=0.3)
    t0 = time.monotonic()
    with pytest.raises(WebSearchTimeout):
        p.search("q")
    assert time.monotonic() - t0 < 2.0  # returned promptly, didn't wait for the hang
    assert p._ddgs is None  # client discarded after timeout


def test_ratelimit_retries_then_raises():
    fake = _FakeDDGS(raise_n_times=99, exc=RatelimitException)
    p = _provider(fake, max_retries=2, min_interval_s=0)
    with pytest.raises(WebSearchRateLimited):
        p.search("q")
    assert fake.calls == 3  # initial + 2 retries


def test_ratelimit_retries_then_succeeds():
    fake = _FakeDDGS(rows=[{"title": "ok", "href": "https://ok", "body": "b"}],
                     raise_n_times=2, exc=RatelimitException)
    p = _provider(fake, max_retries=3, min_interval_s=0)
    hits = p.search("q")
    assert len(hits) == 1 and hits[0].title == "ok"
    assert fake.calls == 3


def test_generic_ddgs_error_maps_to_unavailable():
    fake = _FakeDDGS(raise_n_times=1, exc=DDGSException)
    with pytest.raises(WebSearchUnavailable):
        _provider(fake, max_retries=0).search("q")


def test_supports_and_health():
    p = DdgsSearchProvider({})
    assert p.supports("full_text") is False
    assert p.supports("time_filter") is True
    h = p.health()
    assert h["ok"] is True and h["needs_key"] is False


def test_min_interval_spacing_enforced():
    fake = _FakeDDGS(rows=[])
    p = _provider(fake, min_interval_s=0.4)
    t0 = time.monotonic()
    p.search("a")
    p.search("b")
    # Second call waits ~min_interval after the first.
    assert time.monotonic() - t0 >= 0.35
