"""Unit tests for ``work_buddy.knowledge.editor``.

Today these tests focus on the placeholder-recursion hint emitted in
``create_unit`` / ``update_unit`` responses. The hint is informational
only — it never blocks a write — so the surface we exercise is the
``hints`` field in the editor's return dict.

Isolation: each test patches ``_STORE_DIR`` (and the loader's cached
view of it) at the temp path so the real ``knowledge/store/`` on disk
is untouched. ``_invalidate_and_validate`` reloads the store from the
patched directory between writes, so DAG checks see only the
hand-crafted fixture units.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from work_buddy.knowledge import editor as editor_mod
from work_buddy.knowledge import store as store_mod
from work_buddy.knowledge.editor import (
    _scan_placeholder_hints,
    create_unit,
    update_unit,
)
from work_buddy.knowledge.model import DirectionsUnit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the editor + store loader at a fresh temp directory.

    Returns the patched store directory. Tests can seed it with JSON
    files before invoking editor helpers; the editor's writes also land
    here.
    """
    store_dir = tmp_path / "store"
    store_dir.mkdir()

    # Both modules hold their own reference to _STORE_DIR; patch both
    # so writes and the subsequent load_store() see the same path.
    monkeypatch.setattr(editor_mod, "_STORE_DIR", store_dir)
    monkeypatch.setattr(store_mod, "_STORE_DIR", store_dir)

    # Invalidate any cached store state from prior tests.
    store_mod.invalidate_store()

    return store_dir


def _write_units(store_dir: Path, file_stem: str, units: dict[str, dict[str, Any]]) -> None:
    """Helper: dump a units dict as one of the store's JSON files."""
    (store_dir / f"{file_stem}.json").write_text(
        json.dumps(units, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Direct unit tests for the helper — no editor wiring involved
# ---------------------------------------------------------------------------


class TestScanPlaceholderHints:
    def test_no_placeholders_returns_empty(self):
        unit = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "Plain text only."},
        )
        store = {"a": unit}
        assert _scan_placeholder_hints("a", store) == []

    def test_plain_placeholder_at_leaf_returns_empty(self):
        """Plain ``<<wb:b>>`` is fine when B has no placeholders of its own."""
        b = DirectionsUnit(
            path="b", name="B", description="b",
            content={"full": "B's body, plain text."},
        )
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "Pre. <<wb:b>> Post."},
        )
        store = {"a": a, "b": b}
        assert _scan_placeholder_hints("a", store) == []

    def test_plain_placeholder_at_deep_chain_emits_hint(self):
        """The foot-gun the lint exists to catch."""
        c = DirectionsUnit(
            path="c", name="C", description="c",
            content={"full": "C-leaf."},
        )
        b = DirectionsUnit(
            path="b", name="B", description="b",
            content={"full": "B-body <<wb:c>>"},
        )
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "Pre. <<wb:b>> Post."},
        )
        store = {"a": a, "b": b, "c": c}
        hints = _scan_placeholder_hints("a", store)
        assert len(hints) == 1
        assert hints[0]["hint"] == "placeholder_recursion"
        assert hints[0]["placeholder"] == "b"
        assert "--recursive" in hints[0]["message"]

    def test_recursive_flag_suppresses_hint(self):
        """If the author already wrote --recursive, no hint."""
        c = DirectionsUnit(
            path="c", name="C", description="c",
            content={"full": "C-leaf."},
        )
        b = DirectionsUnit(
            path="b", name="B", description="b",
            content={"full": "B-body <<wb:c>>"},
        )
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "Pre. <<wb:b --recursive>> Post."},
        )
        store = {"a": a, "b": b, "c": c}
        assert _scan_placeholder_hints("a", store) == []

    def test_dedupe_repeated_target(self):
        """Two plain references to the same deep-chain target → one hint."""
        b = DirectionsUnit(
            path="b", name="B", description="b",
            content={"full": "<<wb:other>>"},
        )
        other = DirectionsUnit(
            path="other", name="Other", description="other",
            content={"full": "leaf"},
        )
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "<<wb:b>> mid <<wb:b>>"},
        )
        store = {"a": a, "b": b, "other": other}
        hints = _scan_placeholder_hints("a", store)
        assert len(hints) == 1
        assert hints[0]["placeholder"] == "b"

    def test_multiple_distinct_deep_targets_each_get_hint(self):
        b = DirectionsUnit(
            path="b", name="B", description="b",
            content={"full": "<<wb:x>>"},
        )
        c = DirectionsUnit(
            path="c", name="C", description="c",
            content={"full": "<<wb:y>>"},
        )
        x = DirectionsUnit(path="x", name="X", description="x", content={"full": "x"})
        y = DirectionsUnit(path="y", name="Y", description="y", content={"full": "y"})
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "<<wb:b>> and <<wb:c>>"},
        )
        store = {"a": a, "b": b, "c": c, "x": x, "y": y}
        hints = _scan_placeholder_hints("a", store)
        assert {h["placeholder"] for h in hints} == {"b", "c"}

    def test_missing_target_does_not_emit_hint(self):
        """Broken refs are the resolver's problem to surface, not ours."""
        a = DirectionsUnit(
            path="a", name="A", description="a",
            content={"full": "<<wb:nonexistent/path>>"},
        )
        store = {"a": a}
        assert _scan_placeholder_hints("a", store) == []

    def test_unknown_unit_path_returns_empty(self):
        """Helper is robust to being called with a path not in the store."""
        store: dict = {}
        assert _scan_placeholder_hints("a", store) == []


# ---------------------------------------------------------------------------
# Editor surface — create_unit / update_unit return ``hints``
# ---------------------------------------------------------------------------


class TestCreateUnitHints:
    def test_create_with_no_placeholders_returns_empty_hints(self, tmp_store: Path):
        result = create_unit(
            path="a/leaf",
            kind="directions",
            name="Leaf",
            description="leaf",
            content_full="Plain text body.",
            trigger="never",
        )
        assert result["status"] == "created"
        assert "hints" in result
        assert result["hints"] == []

    def test_create_with_deep_chain_foot_gun_emits_hint(self, tmp_store: Path):
        # Pre-seed a target unit that itself has a placeholder.
        _write_units(tmp_store, "seed", {
            "seed/leaf": {
                "kind": "directions",
                "name": "Leaf",
                "description": "leaf",
                "trigger": "never",
                "content": {"full": "leaf content"},
                "parents": [],
                "children": [],
            },
            "seed/mid": {
                "kind": "directions",
                "name": "Mid",
                "description": "mid",
                "trigger": "never",
                "content": {"full": "Mid says: <<wb:seed/leaf>>"},
                "parents": [],
                "children": [],
            },
        })
        store_mod.invalidate_store()

        result = create_unit(
            path="seed/top",
            kind="directions",
            name="Top",
            description="top",
            content_full="Top says: <<wb:seed/mid>>",
            trigger="never",
        )

        assert result["status"] == "created"
        assert len(result["hints"]) == 1
        h = result["hints"][0]
        assert h["hint"] == "placeholder_recursion"
        assert h["placeholder"] == "seed/mid"


class TestUpdateUnitHints:
    def _seed(self, tmp_store: Path) -> None:
        _write_units(tmp_store, "seed", {
            "seed/leaf": {
                "kind": "directions",
                "name": "Leaf",
                "description": "leaf",
                "trigger": "never",
                "content": {"full": "leaf"},
                "parents": [],
                "children": [],
            },
            "seed/mid": {
                "kind": "directions",
                "name": "Mid",
                "description": "mid",
                "trigger": "never",
                "content": {"full": "Mid: <<wb:seed/leaf>>"},
                "parents": [],
                "children": [],
            },
            "seed/top": {
                "kind": "directions",
                "name": "Top",
                "description": "top",
                "trigger": "never",
                "content": {"full": "Initial body."},
                "parents": [],
                "children": [],
            },
        })
        store_mod.invalidate_store()

    def test_update_introduces_foot_gun_surfaces_hint(self, tmp_store: Path):
        self._seed(tmp_store)
        result = update_unit("seed/top", {"content_full": "Now: <<wb:seed/mid>>"})
        assert result["status"] == "updated"
        assert len(result["hints"]) == 1
        assert result["hints"][0]["placeholder"] == "seed/mid"

    def test_update_with_recursive_flag_no_hint(self, tmp_store: Path):
        self._seed(tmp_store)
        result = update_unit(
            "seed/top",
            {"content_full": "Now: <<wb:seed/mid --recursive>>"},
        )
        assert result["status"] == "updated"
        assert result["hints"] == []

    def test_update_to_plain_text_clears_hint(self, tmp_store: Path):
        self._seed(tmp_store)
        # First, introduce the foot-gun
        update_unit("seed/top", {"content_full": "<<wb:seed/mid>>"})
        # Then strip it
        result = update_unit("seed/top", {"content_full": "Plain again."})
        assert result["hints"] == []

    def test_hints_field_always_present(self, tmp_store: Path):
        """Even when no placeholders exist at all, the key is in the dict
        so downstream consumers can rely on its presence."""
        self._seed(tmp_store)
        result = update_unit("seed/leaf", {"description": "leaf (renamed)"})
        assert "hints" in result
        assert result["hints"] == []
