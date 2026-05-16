"""Tests for fix_params current-value enrichment.

`fix_params_with_current_values` lets the dashboard fix form pre-fill a
requirement's live configured value (so the form doubles as an editor),
by resolving each field's declared `config_path` against the merged
config. Secret fields declare no `config_path` and stay un-enriched.
"""

from __future__ import annotations

from work_buddy.config import load_config
from work_buddy.health.requirements import (
    REQUIREMENT_REGISTRY,
    _config_value_at,
    fix_params_with_current_values,
)


class TestConfigValueAt:
    def test_top_level_key(self):
        assert _config_value_at({"timezone": "UTC"}, "timezone") == "UTC"

    def test_nested_dotted_path(self):
        cfg = {"backups": {"github": {"repo": "me/data"}}}
        assert _config_value_at(cfg, "backups.github.repo") == "me/data"

    def test_missing_path_returns_none(self):
        assert _config_value_at({"a": {}}, "a.b.c") is None
        assert _config_value_at({}, "nope") is None

    def test_non_dict_hop_returns_none(self):
        assert _config_value_at({"a": "scalar"}, "a.b") is None


class TestFixParamsCurrentValues:
    def test_config_path_field_gets_current_value(self):
        req = REQUIREMENT_REGISTRY["core/config/timezone"]
        enriched = fix_params_with_current_values(req)
        assert enriched["timezone"]["current_value"] == load_config().get("timezone")

    def test_secret_field_without_config_path_not_enriched(self):
        # The Telegram bot token is input_required but secret — it
        # declares no config_path, so the token is never echoed back.
        req = REQUIREMENT_REGISTRY["services/telegram/bot-token"]
        enriched = fix_params_with_current_values(req)
        assert "current_value" not in enriched["bot_token"]

    def test_does_not_mutate_the_requirement_def(self):
        req = REQUIREMENT_REGISTRY["core/config/timezone"]
        fix_params_with_current_values(req)
        # The shared registry def must stay clean for the next caller.
        assert "current_value" not in req.fix_params["timezone"]

    def test_requirement_without_fix_params(self):
        req = REQUIREMENT_REGISTRY["core/config/config-yaml-exists"]
        assert fix_params_with_current_values(req) == {}
