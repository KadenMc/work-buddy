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


# ---------------------------------------------------------------------------
# 3. Session dev mode state
# ---------------------------------------------------------------------------

class TestDevModeState:
    def test_get_dev_mode_default_false(self, tmp_agents_dir):
        from work_buddy.agent_session import get_dev_mode
        assert get_dev_mode() is False

    def test_set_and_get_dev_mode(self, tmp_agents_dir):
        from work_buddy.agent_session import get_dev_mode, set_dev_mode
        set_dev_mode(True)
        assert get_dev_mode() is True
        set_dev_mode(False)
        assert get_dev_mode() is False

    def test_update_manifest_preserves_existing(self, tmp_agents_dir):
        from work_buddy.agent_session import get_session_dir, update_manifest
        session_dir = get_session_dir()
        manifest_path = session_dir / "manifest.json"

        # Read original manifest
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert "session_id" in original

        # Update with dev_mode
        updated = update_manifest(dev_mode=True)
        assert updated["dev_mode"] is True
        assert updated["session_id"] == original["session_id"]

        # Verify on disk
        on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert on_disk["dev_mode"] is True
        assert on_disk["session_id"] == original["session_id"]

    def test_update_manifest_multiple_fields(self, tmp_agents_dir):
        from work_buddy.agent_session import update_manifest
        result = update_manifest(dev_mode=True, custom_flag="hello")
        assert result["dev_mode"] is True
        assert result["custom_flag"] == "hello"


# ---------------------------------------------------------------------------
# 4. dev_mode_toggle capability
# ---------------------------------------------------------------------------

class TestDevModeToggle:
    def test_toggle_on(self, tmp_agents_dir):
        from work_buddy.mcp_server.registry import _dev_mode_toggle
        result = _dev_mode_toggle(enabled=True)
        assert result["dev_mode"] is True
        assert result["previous"] is False

    def test_toggle_off(self, tmp_agents_dir):
        from work_buddy.agent_session import set_dev_mode
        from work_buddy.mcp_server.registry import _dev_mode_toggle
        set_dev_mode(True)
        result = _dev_mode_toggle(enabled=False)
        assert result["dev_mode"] is False
        assert result["previous"] is True

    def test_toggle_none_flips(self, tmp_agents_dir):
        from work_buddy.mcp_server.registry import _dev_mode_toggle
        # Start False, toggle → True
        result = _dev_mode_toggle(enabled=None)
        assert result["dev_mode"] is True
        assert result["previous"] is False
        # Toggle again → False
        result = _dev_mode_toggle(enabled=None)
        assert result["dev_mode"] is False
        assert result["previous"] is True

    def test_toggle_omitted_same_as_none(self, tmp_agents_dir):
        from work_buddy.mcp_server.registry import _dev_mode_toggle
        result = _dev_mode_toggle()
        assert result["dev_mode"] is True  # was False, toggled
