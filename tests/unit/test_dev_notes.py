"""Tests for the dev_notes system.

Covers:
- KnowledgeUnit.dev_notes field: schema, tier() gating, serialization
- Session dev mode: manifest update, get/set helpers
- Query pipeline: dev param threading
"""

import json
from pathlib import Path

import pytest

from work_buddy.knowledge.model import (
    DirectionsUnit,
    KnowledgeUnit,
    PromptUnit,
    SystemUnit,
    unit_from_dict,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_unit(dev_notes: str = "", **overrides) -> DirectionsUnit:
    """Create a minimal DirectionsUnit for testing."""
    defaults = {
        "path": "test/unit",
        "name": "Test Unit",
        "description": "A test unit",
        "content": {"summary": "Short summary", "full": "Full content here"},
        "dev_notes": dev_notes,
        "trigger": "test",
    }
    defaults.update(overrides)
    return DirectionsUnit(**defaults)


# ---------------------------------------------------------------------------
# 1. tier() — dev_notes gating
# ---------------------------------------------------------------------------

class TestTierDevNotes:
    def test_tier_excludes_dev_notes_by_default(self):
        unit = _make_unit(dev_notes="Secret dev info")
        result = unit.tier("full")
        assert "dev_notes" not in result

    def test_tier_includes_dev_notes_when_dev_true(self):
        unit = _make_unit(dev_notes="Secret dev info")
        result = unit.tier("full", dev=True)
        assert result["dev_notes"] == "Secret dev info"

    def test_tier_no_dev_notes_at_summary_without_dev(self):
        unit = _make_unit(dev_notes="Secret dev info")
        result = unit.tier("summary")
        assert "dev_notes" not in result
        assert result.get("has_dev_notes") is True

    def test_tier_has_dev_notes_hint_at_summary(self):
        """Summary depth shows has_dev_notes=True hint when notes exist."""
        unit = _make_unit(dev_notes="Some notes")
        result = unit.tier("summary")
        assert result["has_dev_notes"] is True

    def test_tier_no_hint_when_empty(self):
        """No has_dev_notes hint when dev_notes is empty."""
        unit = _make_unit(dev_notes="")
        result = unit.tier("summary")
        assert "has_dev_notes" not in result

    def test_tier_empty_dev_notes_omitted_even_with_dev(self):
        """dev=True but empty dev_notes → no dev_notes key in output."""
        unit = _make_unit(dev_notes="")
        result = unit.tier("full", dev=True)
        assert "dev_notes" not in result

    def test_tier_index_depth_never_shows_dev_notes(self):
        unit = _make_unit(dev_notes="Secret dev info")
        result = unit.tier("index")
        assert "dev_notes" not in result
        assert "has_dev_notes" not in result

    def test_tier_dev_false_explicit(self):
        """Explicit dev=False behaves like default."""
        unit = _make_unit(dev_notes="Secret dev info")
        result = unit.tier("full", dev=False)
        assert "dev_notes" not in result


# ---------------------------------------------------------------------------
# 2. Serialization roundtrip
# ---------------------------------------------------------------------------

class TestDevNotesSerialization:
    def test_to_dict_includes_dev_notes(self):
        unit = _make_unit(dev_notes="Important dev info")
        d = unit.to_dict()
        assert d["dev_notes"] == "Important dev info"

    def test_to_dict_omits_empty_dev_notes(self):
        unit = _make_unit(dev_notes="")
        d = unit.to_dict()
        assert "dev_notes" not in d

    def test_unit_from_dict_loads_dev_notes(self):
        data = {
            "kind": "directions",
            "name": "Test",
            "description": "Test desc",
            "content": {"full": "Content"},
            "dev_notes": "Loaded from JSON",
            "trigger": "test",
        }
        unit = unit_from_dict("test/roundtrip", data)
        assert unit.dev_notes == "Loaded from JSON"

    def test_unit_from_dict_defaults_empty(self):
        data = {
            "kind": "directions",
            "name": "Test",
            "description": "Test desc",
            "trigger": "test",
        }
        unit = unit_from_dict("test/no-notes", data)
        assert unit.dev_notes == ""

    def test_full_roundtrip(self):
        """Serialize → dict → deserialize preserves dev_notes."""
        original = _make_unit(dev_notes="Roundtrip test")
        d = original.to_dict()
        restored = unit_from_dict(original.path, d)
        assert restored.dev_notes == "Roundtrip test"
