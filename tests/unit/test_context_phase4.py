"""Phase-4 unit tests — :mod:`work_buddy.context` types + cache + registry.

No real source implementations yet (those land in phase 5). Tests
focus on the primitives: bucket key stability, cache roundtrip, the
``is_stale`` override contract, and registry idempotence.
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

import pytest

from work_buddy.context import (
    BaseContextSource,
    Context,
    ContextDepth,
    ContextRequest,
    ContextSection,
)
from work_buddy.context import cache as cache_mod
from work_buddy.context import registry


# ---------------------------------------------------------------------------
# Test source — minimal in-process implementation.
# ---------------------------------------------------------------------------


class _FakeSource(BaseContextSource):
    name = "fake"

    def __init__(self, items=None, stale=False):
        self._items = items or ["a", "b"]
        self._stale = stale
        self.collect_calls = 0

    def collect(self, request):
        self.collect_calls += 1
        return ContextSection(source=self.name, items=list(self._items))

    def render(self, section, depth):
        if depth == ContextDepth.BRIEF:
            return f"{section.source}: {len(section.items)} items"
        return "\n".join(f"- {i}" for i in section.items)

    def is_stale(self, cached, request):
        return self._stale


# ---------------------------------------------------------------------------
# ContextDepth + ContextRequest
# ---------------------------------------------------------------------------


class TestContextDepth:
    def test_ordinal(self):
        assert ContextDepth.BRIEF < ContextDepth.NORMAL < ContextDepth.DEEP
        assert ContextDepth.DEEP >= ContextDepth.NORMAL


class TestContextRequest:
    def test_defaults(self):
        req = ContextRequest()
        assert req.sources is None
        assert req.depth is ContextDepth.NORMAL
        assert req.window_days == 1
        assert req.max_age_seconds is None

    def test_depth_for_falls_back_to_global(self):
        req = ContextRequest(depth=ContextDepth.BRIEF)
        assert req.depth_for("git") is ContextDepth.BRIEF

    def test_depth_for_respects_per_source_override(self):
        req = ContextRequest(
            depth=ContextDepth.BRIEF,
            per_source_depth={"git": ContextDepth.DEEP},
        )
        assert req.depth_for("git") is ContextDepth.DEEP
        assert req.depth_for("tasks") is ContextDepth.BRIEF

    def test_custom_for_returns_copy(self):
        # Callers should never be able to mutate the request's state.
        req = ContextRequest(custom={"git": {"detail_days": 7}})
        got = req.custom_for("git")
        got["detail_days"] = 999
        assert req.custom_for("git")["detail_days"] == 7

    def test_custom_for_unknown_source_returns_empty(self):
        assert ContextRequest().custom_for("nope") == {}


# ---------------------------------------------------------------------------
# ContextSection roundtrip
# ---------------------------------------------------------------------------


class TestContextSectionRoundtrip:
    def test_to_from_dict(self):
        s = ContextSection(source="x", items=[{"k": 1}, "two"], metadata={"n": 2})
        d = s.to_dict()
        assert d["source"] == "x"
        assert d["items"] == [{"k": 1}, "two"]
        # Roundtrip
        back = ContextSection.from_dict(d)
        assert back.source == "x"
        assert back.items == [{"k": 1}, "two"]
        assert back.metadata == {"n": 2}

    def test_from_dict_tolerates_missing_fields(self):
        back = ContextSection.from_dict({"source": "x"})
        assert back.source == "x"
        assert back.items == []
        assert back.metadata == {}


# ---------------------------------------------------------------------------
# Bucket key stability
# ---------------------------------------------------------------------------


class TestBucketKey:
    def test_stable_across_calls(self):
        req = ContextRequest(target_date=date(2026, 4, 20), window_days=7)
        a = cache_mod.bucket_key("git", req)
        b = cache_mod.bucket_key("git", req)
        assert a == b
        assert len(a) == 16

    def test_differs_by_target_date(self):
        r1 = ContextRequest(target_date=date(2026, 4, 20))
        r2 = ContextRequest(target_date=date(2026, 4, 21))
        assert cache_mod.bucket_key("git", r1) != cache_mod.bucket_key("git", r2)

    def test_differs_by_custom_params(self):
        r1 = ContextRequest(custom={"git": {"detail_days": 7}})
        r2 = ContextRequest(custom={"git": {"detail_days": 30}})
        assert cache_mod.bucket_key("git", r1) != cache_mod.bucket_key("git", r2)

    def test_ignores_non_fetch_params(self):
        # Depth and max_chars are rendering — bucket shouldn't branch on them.
        r1 = ContextRequest(depth=ContextDepth.BRIEF, max_chars=500)
        r2 = ContextRequest(depth=ContextDepth.DEEP, max_chars=None)
        assert cache_mod.bucket_key("git", r1) == cache_mod.bucket_key("git", r2)

    def test_source_name_affects_key(self):
        req = ContextRequest()
        assert cache_mod.bucket_key("git", req) != cache_mod.bucket_key("tasks", req)


# ---------------------------------------------------------------------------
# Cache read / write / freshness
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_cache_root(tmp_path, monkeypatch):
    # Redirect data_dir to a tmp path so tests don't touch real caches.
    monkeypatch.setattr(cache_mod, "_cache_root", lambda: tmp_path)
    return tmp_path


class TestCacheRoundtrip:
    def test_read_missing_returns_none(self, tmp_cache_root):
        assert cache_mod.read_cached("git", "nonexistent") is None

    def test_roundtrip_preserves_data(self, tmp_cache_root):
        section = ContextSection(
            source="git",
            items=[{"sha": "abc", "msg": "hi"}],
            metadata={"head": "abc"},
        )
        path = cache_mod.write_cached(section, "bucket1")
        assert path.exists()
        loaded = cache_mod.read_cached("git", "bucket1")
        assert loaded is not None
        assert loaded.source == "git"
        assert loaded.items == [{"sha": "abc", "msg": "hi"}]
        assert loaded.metadata == {"head": "abc"}

    def test_write_is_atomic_leaves_no_tmp(self, tmp_cache_root):
        section = ContextSection(source="git", items=["x"])
        cache_mod.write_cached(section, "b")
        tmp_files = list(tmp_cache_root.rglob("*.tmp"))
        assert tmp_files == []

    def test_meta_sidecar_written(self, tmp_cache_root):
        section = ContextSection(source="git", items=["x"])
        cache_mod.write_cached(section, "b")
        meta_files = list(tmp_cache_root.rglob("*.meta.json"))
        assert len(meta_files) == 1
        data = json.loads(meta_files[0].read_text())
        assert data["request_fingerprint"] == "b"
        assert data["version"] == 1


class TestFreshness:
    def test_no_cache_never_fresh(self, tmp_cache_root):
        assert cache_mod.is_fresh_enough("git", "missing", 3600) is False

    def test_none_max_age_never_fresh(self, tmp_cache_root):
        cache_mod.write_cached(ContextSection(source="git", items=[]), "b")
        assert cache_mod.is_fresh_enough("git", "b", None) is False

    def test_zero_max_age_always_fresh_when_cached(self, tmp_cache_root):
        cache_mod.write_cached(ContextSection(source="git", items=[]), "b")
        # 0 means "any cached entry is fresh" — `is_fresh_enough`
        # interprets age<=0 as within-window.
        assert cache_mod.is_fresh_enough("git", "b", 0) is True

    def test_within_window_is_fresh(self, tmp_cache_root):
        cache_mod.write_cached(ContextSection(source="git", items=[]), "b")
        assert cache_mod.is_fresh_enough("git", "b", 3600) is True

    def test_beyond_window_not_fresh(self, tmp_cache_root, monkeypatch):
        cache_mod.write_cached(ContextSection(source="git", items=[]), "b")
        # Pretend time moved forward. Capture real time first so the
        # patched function doesn't call itself recursively.
        real_now = time.time()
        monkeypatch.setattr(cache_mod.time, "time", lambda: real_now + 7200)
        assert cache_mod.is_fresh_enough("git", "b", 3600) is False


class TestEvict:
    def test_evict_specific_bucket(self, tmp_cache_root):
        cache_mod.write_cached(ContextSection(source="git", items=["a"]), "b1")
        cache_mod.write_cached(ContextSection(source="git", items=["b"]), "b2")
        n = cache_mod.evict("git", "b1")
        assert n == 1
        assert cache_mod.read_cached("git", "b1") is None
        assert cache_mod.read_cached("git", "b2") is not None

    def test_evict_all_for_source(self, tmp_cache_root):
        cache_mod.write_cached(ContextSection(source="git", items=["a"]), "b1")
        cache_mod.write_cached(ContextSection(source="git", items=["b"]), "b2")
        n = cache_mod.evict("git")
        assert n == 2

    def test_evict_absent_source_is_zero(self, tmp_cache_root):
        assert cache_mod.evict("nope") == 0


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    # Snapshot + restore rather than just clear — the real wave-1 sources
    # register at import time (git/tasks/projects/chrome), and dropping
    # them permanently breaks tests further down the suite that depend
    # on their registration.

    def setup_method(self):
        self._snapshot = registry.all_sources()
        registry.clear()

    def teardown_method(self):
        registry.clear()
        for name, src in self._snapshot.items():
            registry.register(src)

    def test_register_and_get(self):
        src = _FakeSource()
        registry.register(src)
        assert registry.get("fake") is src
        assert "fake" in registry.names()

    def test_register_is_idempotent_replaces(self):
        a = _FakeSource()
        b = _FakeSource()
        registry.register(a)
        registry.register(b)
        assert registry.get("fake") is b
        assert len(registry.names()) == 1

    def test_unregister(self):
        src = _FakeSource()
        registry.register(src)
        assert registry.unregister("fake") is src
        assert registry.get("fake") is None

    def test_all_sources_returns_copy(self):
        registry.register(_FakeSource())
        snap = registry.all_sources()
        snap.pop("fake", None)
        assert "fake" in registry.names()  # registry unaffected


# ---------------------------------------------------------------------------
# BaseContextSource defaults
# ---------------------------------------------------------------------------


class TestBaseContextSource:
    def test_is_stale_default_false(self):
        src = _FakeSource()
        cached = ContextSection(source="fake", items=["x"])
        assert src.is_stale(cached, ContextRequest()) is False

    def test_drill_down_default_raises(self):
        class _Bare(BaseContextSource):
            name = "bare"

            def collect(self, request):
                return ContextSection(source=self.name)

            def render(self, section, depth):
                return ""

        with pytest.raises(NotImplementedError, match="context_drill_down"):
            _Bare().drill_down("id", "field")


# ---------------------------------------------------------------------------
# Context container
# ---------------------------------------------------------------------------


class TestContext:
    def test_empty(self):
        ctx = Context()
        assert ctx.section("git") is None
        assert ctx.has("git") is False

    def test_section_lookup(self):
        section = ContextSection(source="git", items=["x"])
        ctx = Context(sections={"git": section})
        assert ctx.section("git") is section
        assert ctx.has("git") is True

    def test_empty_section_doesnt_count_as_has(self):
        # An explicitly-empty section is valid cache content (source had
        # nothing to report) but `has()` is the "is there content" check.
        ctx = Context(sections={"git": ContextSection(source="git", items=[])})
        assert ctx.has("git") is False
