"""Unit tests for the feature preferences system (health/preferences.py)."""

import pytest
import yaml

from work_buddy.health.preferences import (
    FeaturePreference,
    get_preference,
    is_wanted,
    load_preferences,
    save_preferences,
    set_preference,
)


class TestFeaturePreference:
    def test_default_wanted_is_none(self):
        pref = FeaturePreference(component_id="obsidian")
        assert pref.wanted is None
        assert pref.reason is None

    def test_to_dict_minimal(self):
        pref = FeaturePreference(component_id="obsidian", wanted=True)
        d = pref.to_dict()
        assert d == {"wanted": True}
        assert "reason" not in d

    def test_to_dict_with_reason(self):
        pref = FeaturePreference(
            component_id="hindsight", wanted=False, reason="Not using"
        )
        d = pref.to_dict()
        assert d == {"wanted": False, "reason": "Not using"}

    def test_from_dict(self):
        pref = FeaturePreference.from_dict("telegram", {"wanted": False, "reason": "No bot"})
        assert pref.component_id == "telegram"
        assert pref.wanted is False
        assert pref.reason == "No bot"

    def test_from_dict_missing_fields(self):
        pref = FeaturePreference.from_dict("obsidian", {})
        assert pref.wanted is None
        assert pref.reason is None


class TestLoadSavePreferences:
    def test_load_empty_when_no_features_section(self, tmp_path, monkeypatch):
        """No 'features:' in config.local.yaml → empty dict."""
        local = tmp_path / "config.local.yaml"
        local.write_text("dashboard:\n  external_url: x\n", encoding="utf-8")
        monkeypatch.setattr(
            "work_buddy.health.preferences.read_config_local",
            lambda: yaml.safe_load(local.read_text(encoding="utf-8")) or {},
        )
        prefs = load_preferences()
        assert prefs == {}

    def test_load_empty_when_no_file(self, monkeypatch):
        """config.local.yaml doesn't exist → empty dict."""
        monkeypatch.setattr(
            "work_buddy.health.preferences.read_config_local",
            lambda: {},
        )
        prefs = load_preferences()
        assert prefs == {}

    def test_load_parses_features(self, monkeypatch):
        monkeypatch.setattr(
            "work_buddy.health.preferences.read_config_local",
            lambda: {
                "features": {
                    "hindsight": {"wanted": False, "reason": "Not using"},
                    "obsidian": {"wanted": True},
                }
            },
        )
        prefs = load_preferences()
        assert len(prefs) == 2
        assert prefs["hindsight"].wanted is False
        assert prefs["hindsight"].reason == "Not using"
        assert prefs["obsidian"].wanted is True

    def test_load_handles_bare_bool(self, monkeypatch):
        """features.telegram: false (bare bool, not dict)."""
        monkeypatch.setattr(
            "work_buddy.health.preferences.read_config_local",
            lambda: {"features": {"telegram": False}},
        )
        prefs = load_preferences()
        assert prefs["telegram"].wanted is False

    def test_save_roundtrip(self, tmp_path, monkeypatch):
        local_file = tmp_path / "config.local.yaml"
        local_file.write_text("dashboard:\n  external_url: x\n", encoding="utf-8")

        # Patch read/write to use tmp file
        def _read():
            if local_file.exists():
                return yaml.safe_load(local_file.read_text(encoding="utf-8")) or {}
            return {}

        def _write(section, data):
            existing = _read()
            existing[section] = data
            local_file.write_text(
                yaml.safe_dump(existing, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )

        monkeypatch.setattr("work_buddy.health.preferences.read_config_local", _read)
        monkeypatch.setattr("work_buddy.health.preferences.write_config_local", _write)

        # Save preferences
        prefs = {
            "hindsight": FeaturePreference("hindsight", wanted=False, reason="No DB"),
            "obsidian": FeaturePreference("obsidian", wanted=True),
        }
        save_preferences(prefs)

        # Read back
        loaded = load_preferences()
        assert loaded["hindsight"].wanted is False
        assert loaded["hindsight"].reason == "No DB"
        assert loaded["obsidian"].wanted is True

        # Verify other sections preserved
        data = yaml.safe_load(local_file.read_text(encoding="utf-8"))
        assert data["dashboard"]["external_url"] == "x"


class TestGetPreference:
    def test_returns_undecided_for_unknown(self, monkeypatch):
        monkeypatch.setattr(
            "work_buddy.health.preferences.read_config_local",
            lambda: {},
        )
        pref = get_preference("nonexistent")
        assert pref.wanted is None
        assert pref.component_id == "nonexistent"

    def test_returns_explicit_preference(self, monkeypatch):
        monkeypatch.setattr(
            "work_buddy.health.preferences.read_config_local",
            lambda: {"features": {"hindsight": {"wanted": False}}},
        )
        pref = get_preference("hindsight")
        assert pref.wanted is False


class TestIsWanted:
    def test_none_for_undecided(self, monkeypatch):
        monkeypatch.setattr(
            "work_buddy.health.preferences.read_config_local",
            lambda: {},
        )
        assert is_wanted("obsidian") is None

    def test_false_for_opted_out(self, monkeypatch):
        monkeypatch.setattr(
            "work_buddy.health.preferences.read_config_local",
            lambda: {"features": {"hindsight": {"wanted": False}}},
        )
        assert is_wanted("hindsight") is False

    def test_true_for_explicitly_wanted(self, monkeypatch):
        monkeypatch.setattr(
            "work_buddy.health.preferences.read_config_local",
            lambda: {"features": {"obsidian": {"wanted": True}}},
        )
        assert is_wanted("obsidian") is True


class TestSetPreference:
    def test_set_persists(self, tmp_path, monkeypatch):
        local_file = tmp_path / "config.local.yaml"
        local_file.write_text("{}", encoding="utf-8")

        def _read():
            return yaml.safe_load(local_file.read_text(encoding="utf-8")) or {}

        def _write(section, data):
            existing = _read()
            existing[section] = data
            local_file.write_text(
                yaml.safe_dump(existing, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )

        monkeypatch.setattr("work_buddy.health.preferences.read_config_local", _read)
        monkeypatch.setattr("work_buddy.health.preferences.write_config_local", _write)

        set_preference("telegram", wanted=False, reason="No bot")

        loaded = load_preferences()
        assert loaded["telegram"].wanted is False
        assert loaded["telegram"].reason == "No bot"
