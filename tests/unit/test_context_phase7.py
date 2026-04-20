"""Phase-7 unit tests — MCP surface for context (``context_block`` + ``context_drill_down``).

The actual MCP-registration plumbing is tested elsewhere; here we
exercise the callable functions directly to make sure their input
validation, source lookups, and error shapes behave.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.context import registry
from work_buddy.context.types import ContextSection
from work_buddy.mcp_server.registry import _context_block, _context_drill_down


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_cache_root(tmp_path, monkeypatch):
    import work_buddy.context.cache as cache_mod
    monkeypatch.setattr(cache_mod, "_cache_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def clean_registry():
    snap = registry.all_sources()
    registry.clear()
    yield
    registry.clear()
    for name, src in snap.items():
        registry.register(src)


class _StubSource:
    def __init__(self, name, markdown="", drill=None):
        self.name = name
        self._md = markdown
        self._drill = drill or {}

    def collect(self, request):
        return ContextSection(
            source=self.name,
            items=[{"markdown": self._md, "length": len(self._md)}] if self._md else [],
        )

    def render(self, section, depth):
        if not section.items:
            return ""
        return section.items[0]["markdown"]

    def is_stale(self, cached, request):
        return False

    def drill_down(self, item_id, field):
        if (item_id, field) in self._drill:
            return self._drill[(item_id, field)]
        if item_id == "__raise_not_impl__":
            raise NotImplementedError("nope")
        raise KeyError(f"No item {item_id!r} with field {field!r}")


# ---------------------------------------------------------------------------
# context_block
# ---------------------------------------------------------------------------


class TestContextBlock:
    def test_happy_path_returns_rendered(
        self, tmp_cache_root, clean_registry,
    ):
        registry.register(_StubSource("alpha", markdown="# Alpha\n- one"))
        registry.register(_StubSource("beta", markdown="# Beta\n- two"))

        result = _context_block(sources=["alpha", "beta"])
        assert "rendered" in result
        assert "- one" in result["rendered"]
        assert "- two" in result["rendered"]
        assert result["format"] == "markdown"
        assert set(result["sources"].keys()) == {"alpha", "beta"}
        assert result["sources"]["alpha"]["item_count"] == 1

    def test_depth_string_validated(self, tmp_cache_root, clean_registry):
        r = _context_block(depth="gigantic")
        assert "error" in r
        assert "brief" in r["error"]

    def test_invalid_target_date(self, tmp_cache_root, clean_registry):
        r = _context_block(target_date="2026/04/20")
        assert "error" in r
        assert "YYYY-MM-DD" in r["error"]

    def test_invalid_format(self, tmp_cache_root, clean_registry):
        r = _context_block(format="xml")
        assert "error" in r
        assert "markdown" in r["error"]

    def test_per_source_depth_invalid(
        self, tmp_cache_root, clean_registry,
    ):
        registry.register(_StubSource("a", markdown="x"))
        r = _context_block(per_source_depth={"a": "fiery"})
        assert "error" in r
        assert "per_source_depth" in r["error"]

    def test_json_format(self, tmp_cache_root, clean_registry):
        registry.register(_StubSource("a", markdown="# A"))
        r = _context_block(sources=["a"], format="json")
        assert r["format"] == "json"
        import json
        # Parse the rendered JSON to make sure it's valid JSON (not the
        # wrapping dict).
        parsed = json.loads(r["rendered"])
        assert "a" in parsed

    def test_max_chars_truncates(self, tmp_cache_root, clean_registry):
        long_md = "## Big\n" + "\n".join(f"- item {i}" for i in range(100))
        registry.register(_StubSource("bulk", markdown=long_md))
        r = _context_block(sources=["bulk"], max_chars=200)
        assert len(r["rendered"]) <= 220  # truncation marker overhead


# ---------------------------------------------------------------------------
# context_drill_down
# ---------------------------------------------------------------------------


class TestContextDrillDown:
    def test_unknown_source_returns_error(
        self, tmp_cache_root, clean_registry,
    ):
        r = _context_drill_down(source="ghost", item_id="x", field="y")
        assert "error" in r
        assert "Unknown source" in r["error"]

    def test_drill_down_delegates(self, tmp_cache_root, clean_registry):
        src = _StubSource(
            "thing",
            drill={("id-1", "body"): {"id": "id-1", "body": "hello"}},
        )
        registry.register(src)
        r = _context_drill_down(source="thing", item_id="id-1", field="body")
        assert r == {"id": "id-1", "body": "hello"}

    def test_not_implemented_surfaces_cleanly(
        self, tmp_cache_root, clean_registry,
    ):
        src = _StubSource("thing")
        registry.register(src)
        r = _context_drill_down(source="thing", item_id="__raise_not_impl__", field="x")
        assert r["error_kind"] == "not_implemented"

    def test_key_error_surfaces_as_not_found(
        self, tmp_cache_root, clean_registry,
    ):
        src = _StubSource("thing")
        registry.register(src)
        r = _context_drill_down(source="thing", item_id="nope", field="body")
        assert r["error_kind"] == "not_found"

    def test_generic_exception_classified_unknown(
        self, tmp_cache_root, clean_registry,
    ):
        class _Boom(_StubSource):
            def drill_down(self, item_id, field):
                raise RuntimeError("boom")

        registry.register(_Boom("thing"))
        r = _context_drill_down(source="thing", item_id="x", field="y")
        assert r["error_kind"] == "unknown"
        assert "boom" in r["error"]
