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


# ---------------------------------------------------------------------------
# Duplicate placeholders — HARD ERROR (not a hint)
# ---------------------------------------------------------------------------


class TestDuplicatePlaceholderRejection:
    """Duplicate ``<<wb:X>>`` within a unit's ``content.full`` is a hard
    error: the editor refuses the write, returns ``{"error": ...}``,
    and the unit on disk stays unchanged.

    Rationale: at read time the per-unit-occurrence cap renders
    subsequent references as back-reference markers, so duplicates
    contribute zero readable content. There's no legitimate authorial
    case for them.
    """

    def _seed_leaf(self, tmp_store: Path) -> None:
        _write_units(tmp_store, "seed", {
            "seed/leaf": {
                "kind": "directions",
                "name": "Leaf",
                "description": "leaf",
                "trigger": "never",
                "content": {"full": "leaf body"},
                "parents": [],
                "children": [],
            },
            "seed/host": {
                "kind": "directions",
                "name": "Host",
                "description": "host",
                "trigger": "never",
                "content": {"full": "original body"},
                "parents": [],
                "children": [],
            },
        })
        store_mod.invalidate_store()

    def test_create_with_duplicate_placeholder_rejects(self, tmp_store: Path):
        """``create_unit`` refuses to write a unit whose content has
        duplicate placeholders. Disk file is unchanged."""
        self._seed_leaf(tmp_store)
        result = create_unit(
            path="seed/dup",
            kind="directions",
            name="Dup",
            description="dup",
            content_full="<<wb:seed/leaf>> middle <<wb:seed/leaf>>",
            trigger="never",
        )
        assert "error" in result
        assert result["error"] == "placeholder_duplicate"
        assert any(d["placeholder"] == "seed/leaf" for d in result["duplicates"])
        # Confirm the unit was NOT written
        from work_buddy.knowledge.store import load_store
        store_mod.invalidate_store()
        store = load_store()
        assert "seed/dup" not in store

    def test_update_with_duplicate_placeholder_rejects(self, tmp_store: Path):
        """``update_unit`` rejects a content_full that introduces
        duplicates. The original content on disk is preserved."""
        self._seed_leaf(tmp_store)
        result = update_unit(
            "seed/host",
            {"content_full": "<<wb:seed/leaf>> some <<wb:seed/leaf>>"},
        )
        assert "error" in result
        assert result["error"] == "placeholder_duplicate"
        # Original content preserved on disk
        store_mod.invalidate_store()
        from work_buddy.knowledge.store import load_store
        unit = load_store()["seed/host"]
        assert unit.content["full"] == "original body"

    def test_update_unrelated_field_does_not_trigger_check(self, tmp_store: Path):
        """The duplicate check fires ONLY when content_full is being
        updated. Updating tags or description should not re-validate
        existing content."""
        # Set up a unit whose existing content has duplicates (e.g.
        # legacy bypass via direct JSON edit). Updating an unrelated
        # field must not block the edit — the validator surfaces it
        # separately.
        _write_units(tmp_store, "seed", {
            "seed/leaf": {
                "kind": "directions",
                "name": "Leaf",
                "description": "leaf",
                "trigger": "never",
                "content": {"full": "leaf body"},
                "parents": [],
                "children": [],
            },
            "seed/bypass_dup": {
                "kind": "directions",
                "name": "Bypass Dup",
                "description": "unit whose duplicates arrived via direct-JSON bypass",
                "trigger": "never",
                "content": {"full": "<<wb:seed/leaf>> a <<wb:seed/leaf>>"},
                "parents": [],
                "children": [],
            },
        })
        store_mod.invalidate_store()

        # Touching ``description`` should succeed even though content
        # has duplicates — the check only fires on content_full edits.
        result = update_unit(
            "seed/bypass_dup",
            {"description": "renamed via unrelated-field update"},
        )
        assert result.get("status") == "updated"

    def test_message_explains_the_problem_and_the_fix(self, tmp_store: Path):
        """The error response must tell the author exactly what's
        wrong and what to do about it — not a generic error."""
        self._seed_leaf(tmp_store)
        result = create_unit(
            path="seed/dup",
            kind="directions",
            name="Dup",
            description="dup",
            content_full="<<wb:seed/leaf>><<wb:seed/leaf>><<wb:seed/leaf>>",
            trigger="never",
        )
        # Mentions the placeholder by name
        assert "seed/leaf" in result["message"]
        # Mentions the count
        assert "3" in result["message"]
        # Mentions the fix
        assert "remove" in result["message"].lower() or "extras" in result["message"].lower()
