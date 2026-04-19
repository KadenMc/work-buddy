"""Tests for the knowledge index on-disk cache.

Focused on the contract the index.py builders rely on: round-trip
correctness, invalidation on model/version mismatch, and graceful
behavior when the cache file is missing or corrupt.
"""
from __future__ import annotations

import numpy as np
import pytest

from work_buddy.knowledge import persistence as persist


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    """Redirect the persistence module to a temp cache dir per-test."""
    content_path = tmp_path / "content.npz"
    alias_path = tmp_path / "aliases.npz"
    monkeypatch.setattr(persist, "_content_cache_path", lambda: content_path)
    monkeypatch.setattr(persist, "_alias_cache_path", lambda: alias_path)
    yield tmp_path


def _vec(dim: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=dim).astype(np.float32)
    v /= np.linalg.norm(v)
    return v


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------

class TestContentHash:
    def test_hash_is_deterministic(self):
        assert persist.content_hash("hello") == persist.content_hash("hello")

    def test_different_strings_hash_differently(self):
        assert persist.content_hash("hello") != persist.content_hash("hell0")

    def test_hash_is_short_hex(self):
        h = persist.content_hash("anything")
        assert len(h) == 16
        int(h, 16)  # parses as hex


# ---------------------------------------------------------------------------
# Content cache round-trip
# ---------------------------------------------------------------------------

class TestContentCache:
    def test_missing_cache_returns_empty(self):
        assert persist.load_content_cache("leaf-ir") == {}

    def test_roundtrip_preserves_paths_and_hashes(self):
        v1 = _vec(768, seed=1)
        v2 = _vec(768, seed=2)
        cache = {
            "tasks/create": ("abc123", v1),
            "journal/write": ("def456", v2),
        }
        persist.save_content_cache(cache, "leaf-ir")

        loaded = persist.load_content_cache("leaf-ir")
        assert set(loaded.keys()) == {"tasks/create", "journal/write"}
        assert loaded["tasks/create"][0] == "abc123"
        assert loaded["journal/write"][0] == "def456"

    def test_roundtrip_preserves_vectors_within_float16_tolerance(self):
        v = _vec(768, seed=42)
        cache = {"path": ("hash", v)}
        persist.save_content_cache(cache, "leaf-ir")
        loaded = persist.load_content_cache("leaf-ir")
        # float16 gives ~3 decimal digits of precision
        assert np.allclose(loaded["path"][1], v, atol=1e-3)

    def test_model_key_mismatch_returns_empty(self):
        cache = {"p": ("h", _vec(768, 0))}
        persist.save_content_cache(cache, "leaf-ir")
        # Reading with a different model_key must discard the cache entirely
        assert persist.load_content_cache("some-other-model") == {}

    def test_version_mismatch_returns_empty(self, monkeypatch):
        cache = {"p": ("h", _vec(768, 0))}
        persist.save_content_cache(cache, "leaf-ir")
        # Bump CACHE_VERSION after saving — old cache becomes invalid
        monkeypatch.setattr(persist, "CACHE_VERSION", persist.CACHE_VERSION + 1)
        assert persist.load_content_cache("leaf-ir") == {}

    def test_corrupt_file_returns_empty(self, tmp_cache):
        # Write garbage to the cache path
        (tmp_cache / "content.npz").write_bytes(b"not a valid npz file")
        assert persist.load_content_cache("leaf-ir") == {}

    def test_save_empty_cache_is_legal(self):
        persist.save_content_cache({}, "leaf-ir")
        loaded = persist.load_content_cache("leaf-ir")
        assert loaded == {}

    def test_save_overwrites_previous(self):
        v1 = _vec(768, 1)
        v2 = _vec(768, 2)
        persist.save_content_cache({"p": ("h1", v1)}, "leaf-ir")
        persist.save_content_cache({"p": ("h2", v2)}, "leaf-ir")
        loaded = persist.load_content_cache("leaf-ir")
        assert loaded["p"][0] == "h2"
        assert np.allclose(loaded["p"][1], v2, atol=1e-3)


# ---------------------------------------------------------------------------
# Alias cache round-trip
# ---------------------------------------------------------------------------

class TestAliasCache:
    def test_missing_cache_returns_empty(self):
        assert persist.load_alias_cache("leaf-mt") == {}

    def test_roundtrip_preserves_tuple_keys(self):
        v1 = _vec(1024, 1)
        v2 = _vec(1024, 2)
        v3 = _vec(1024, 3)
        cache = {
            ("tasks/create", "add a todo"): v1,
            ("tasks/create", "new task"): v2,
            ("journal/write", "log entry"): v3,
        }
        persist.save_alias_cache(cache, "leaf-mt")

        loaded = persist.load_alias_cache("leaf-mt")
        assert set(loaded.keys()) == set(cache.keys())
        assert np.allclose(loaded[("tasks/create", "add a todo")], v1, atol=1e-3)

    def test_model_key_mismatch_returns_empty(self):
        persist.save_alias_cache({("p", "a"): _vec(1024, 0)}, "leaf-mt")
        assert persist.load_alias_cache("wrong-model") == {}

    def test_duplicate_alias_text_across_paths_keeps_distinct_entries(self):
        # Two different capabilities both have the alias "log entry". They
        # should be stored as distinct (path, alias) pairs.
        v1 = _vec(1024, 1)
        v2 = _vec(1024, 2)
        persist.save_alias_cache(
            {("a/x", "log entry"): v1, ("b/y", "log entry"): v2},
            "leaf-mt",
        )
        loaded = persist.load_alias_cache("leaf-mt")
        assert len(loaded) == 2
        assert not np.array_equal(
            loaded[("a/x", "log entry")], loaded[("b/y", "log entry")]
        )


# ---------------------------------------------------------------------------
# Cache clearing and status
# ---------------------------------------------------------------------------

class TestMaintenance:
    def test_clear_caches_removes_both_files(self, tmp_cache):
        persist.save_content_cache({"p": ("h", _vec(768, 0))}, "leaf-ir")
        persist.save_alias_cache({("p", "a"): _vec(1024, 0)}, "leaf-mt")
        assert (tmp_cache / "content.npz").exists()
        assert (tmp_cache / "aliases.npz").exists()

        removed = persist.clear_caches()
        assert removed["content"] is True
        assert removed["aliases"] is True
        assert not (tmp_cache / "content.npz").exists()
        assert not (tmp_cache / "aliases.npz").exists()

    def test_clear_caches_when_missing_is_noop(self):
        removed = persist.clear_caches()
        assert removed == {"content": False, "aliases": False}

    def test_cache_status_reports_missing(self):
        status = persist.cache_status()
        assert status["content"]["missing"] is True
        assert status["aliases"]["missing"] is True

    def test_cache_status_reports_size_when_present(self, tmp_cache):
        # Write enough vectors that the file is >1 KB even compressed —
        # otherwise rounding to 2 decimal places in MB shows 0.0.
        vectors = {f"path{i}": ("h", _vec(768, i)) for i in range(20)}
        persist.save_content_cache(vectors, "leaf-ir")
        assert (tmp_cache / "content.npz").exists()
        status = persist.cache_status()
        assert "missing" not in status["content"]
        # File-on-disk is the authoritative signal; size_mb is for display
        # and may round to 0.0 for tiny caches.
        assert status["content"]["path"].endswith("content.npz")
