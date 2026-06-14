"""Tests for index/resident.py — generic ResidentCache + registry (injected behavior)."""

from __future__ import annotations

from work_buddy.index.resident import ResidentCache, ResidentCacheRegistry


class _Clock:
    """Manually-advanced monotonic clock for deterministic idle-TTL tests."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TestResidentCache:
    def test_loads_once_and_serves_from_ram(self):
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return [calls["n"]]

        cache = ResidentCache(loader, version_fn=lambda: "v1")
        assert cache.get() == [1]
        assert cache.get() == [1]   # served from RAM, not reloaded
        assert calls["n"] == 1
        assert cache.is_cached()

    def test_version_change_reloads(self):
        calls = {"n": 0}
        version = {"v": "v1"}

        def loader():
            calls["n"] += 1
            return calls["n"]

        cache = ResidentCache(loader, version_fn=lambda: version["v"])
        assert cache.get() == 1
        version["v"] = "v2"          # rebuild bumped the version
        assert cache.get() == 2      # reloaded
        assert calls["n"] == 2

    def test_invalidate_forces_reload(self):
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return calls["n"]

        cache = ResidentCache(loader, version_fn=lambda: "v1")
        cache.get()
        cache.invalidate()
        assert not cache.is_cached()
        cache.get()
        assert calls["n"] == 2

    def test_none_loader_not_cached(self):
        cache = ResidentCache(lambda: None, version_fn=lambda: "v1")
        assert cache.get() is None
        assert not cache.is_cached()

    def test_release_if_idle(self):
        clock = _Clock()
        cache = ResidentCache(lambda: [1], version_fn=lambda: "v1", clock=clock)
        cache.get()
        assert cache.is_cached()
        assert cache.release_if_idle(ttl_s=600) is False  # just loaded
        clock.advance(601)
        assert cache.release_if_idle(ttl_s=600) is True    # now idle
        assert not cache.is_cached()

    def test_version_fn_failure_returns_none(self):
        def boom():
            raise RuntimeError("db down")

        cache = ResidentCache(lambda: [1], version_fn=boom)
        assert cache.get() is None

    def test_get_if_cached_never_loads(self):
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return [calls["n"]]

        cache = ResidentCache(loader, version_fn=lambda: "v1")
        assert cache.get_if_cached() is None   # cold → peek does NOT load
        assert calls["n"] == 0
        assert cache.get() == [1]              # now load
        assert cache.get_if_cached() == [1]    # warm → peek serves from RAM
        assert calls["n"] == 1                 # still only the one load

    def test_get_if_cached_misses_on_stale_version(self):
        version = {"v": "v1"}
        cache = ResidentCache(lambda: 1, version_fn=lambda: version["v"])
        cache.get()
        version["v"] = "v2"                    # a rebuild bumped the version
        assert cache.get_if_cached() is None   # stale cached value counts as absent

    def test_get_if_cached_none_after_invalidate(self):
        cache = ResidentCache(lambda: [1], version_fn=lambda: "v1")
        cache.get()
        cache.invalidate()
        assert cache.get_if_cached() is None


class TestResidentCacheRegistry:
    def test_register_and_get(self):
        reg = ResidentCacheRegistry()
        c = ResidentCache(lambda: 1, version_fn=lambda: "v")
        reg.register("a", c)
        assert reg.get("a") is c
        assert reg.get("missing") is None

    def test_get_or_create_is_idempotent(self):
        reg = ResidentCacheRegistry()
        c1 = reg.get_or_create("k", lambda: 1, lambda: "v")
        c2 = reg.get_or_create("k", lambda: 2, lambda: "v")
        assert c1 is c2

    def test_sweep_idle_releases_idle_caches(self):
        clock = _Clock()
        reg = ResidentCacheRegistry()
        a = ResidentCache(lambda: [1], version_fn=lambda: "v", clock=clock)
        b = ResidentCache(lambda: [2], version_fn=lambda: "v", clock=clock)
        reg.register("a", a)
        reg.register("b", b)
        a.get(); b.get()
        clock.advance(601)
        b.get()  # b touched recently (re-stamps loaded_at)
        released = reg.sweep_idle(ttl_s=600)
        assert "a" in released
        assert "b" not in released

    def test_invalidate_all(self):
        reg = ResidentCacheRegistry()
        a = ResidentCache(lambda: [1], version_fn=lambda: "v")
        reg.register("a", a)
        a.get()
        reg.invalidate_all()
        assert not a.is_cached()
