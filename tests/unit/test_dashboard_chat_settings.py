"""Canonical chat defaults: isolated Settings state and provider doubles only."""

from types import SimpleNamespace

import pytest

from work_buddy.agent_execution import registry as execution_registry
from work_buddy.agent_execution.models import (
    AgentExecutionSelection,
    ProviderUnavailableError,
    UnknownModelError,
    UnknownProviderError,
)
from work_buddy.settings import broker, registry, store

SETTING = registry.DASHBOARD_CHAT_EXECUTION_DEFAULT_ID
SONNET = {"provider_id": "claude-code", "model_id": "sonnet"}
OPUS = {"provider_id": "claude-code", "model_id": "opus"}
CODEX = {"provider_id": "codex", "model_id": "fixture-codex-model"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "settings.db")
    monkeypatch.setattr(execution_registry, "_registry", None)
    monkeypatch.setattr(execution_registry, "load_config", dict)
    monkeypatch.setattr(broker, "publish_change", lambda event: None)


@pytest.fixture
def validated(monkeypatch):
    calls = []

    def validate(selection, *, refresh=False):
        calls.append((selection, refresh))
        return selection

    monkeypatch.setattr(execution_registry, "validate_selection", validate)
    return calls


def value():
    payload, _ = broker.get_values(context_id=registry.DASHBOARD_AI_CONTEXT_ID)
    return next(item for item in payload["values"] if item["setting_id"] == SETTING)


def test_dashboard_ai_is_one_system_page_and_tier_value_stays_dormant():
    payload = broker.get_registry()
    page = next(item for item in payload["pages"] if item["page_id"] == registry.DASHBOARD_AI_CONTEXT_ID)
    assert page["navigation_group"] == "system"
    assert page["context"]["kind"] == "system"
    assert page["route"] == "/app/settings/system/dashboard-ai"
    assert not any(item["route"] == "/app/settings/apps/dashboard" for item in payload["pages"])
    assert not any(item["setting_id"] == registry.DASHBOARD_ASSISTANCE_TIER_ID for item in payload["placements"])
    broker.update_value(registry.DASHBOARD_ASSISTANCE_TIER_ID, scope="profile", value="frontier_best", expected_revision="value:0")
    broker.update_value(registry.DASHBOARD_ASSISTANCE_ID, scope="profile", value="enabled", expected_revision="value:0")
    assert broker.get_dashboard_assistance_settings() == {"enabled": True, "tier": "frontier_best"}
    assert broker.get_dashboard_chat_execution_default() == SONNET


def test_default_bootstrap_is_probe_free_and_frozen_across_config_change(monkeypatch, validated):
    monkeypatch.setattr(execution_registry, "load_config", lambda: {"sidecar": {"agent_spawn": {"model": "opus"}}})
    assert broker.get_dashboard_chat_execution_default() == OPUS
    assert value()["default_source"] == "config-bootstrap"
    monkeypatch.setattr(execution_registry, "load_config", lambda: {"sidecar": {"agent_spawn": {"model": "sonnet"}}})
    assert broker.get_dashboard_chat_execution_default() == OPUS
    assert validated == []
    updated, _ = broker.update_value(SETTING, scope="profile", value=CODEX, expected_revision="value:0")
    assert updated["effective_value"] == CODEX
    assert updated["pending_value"] is None
    reset, _ = broker.reset_value(SETTING, scope="profile", expected_revision=updated["revision"])
    assert reset["effective_value"] == OPUS
    assert reset["source"] == "default"
    assert [(selection.provider_id, selection.model_id, refresh) for selection, refresh in validated] == [
        ("codex", CODEX["model_id"], True), ("claude-code", "opus", True),
    ]


@pytest.mark.parametrize("invalid", [None, "sonnet", {}, {"provider_id": "codex"}, {**SONNET, "model_label": "Forged"}, {**SONNET, "model_id": " "}, {**SONNET, "model_id": 7}, {**SONNET, "provider_id": " codex"}, {**SONNET, "model_id": "m" * 257}])
def test_model_pair_is_closed_and_strict_before_provider_probe(invalid, validated):
    with pytest.raises(broker.SettingsError) as error:
        broker.update_value(SETTING, scope="profile", value=invalid, expected_revision="value:0")
    assert error.value.code == "validation_error"
    assert validated == []
    assert broker.get_dashboard_chat_execution_default() == SONNET


@pytest.mark.parametrize("failure", [UnknownProviderError, UnknownModelError, ProviderUnavailableError, RuntimeError])
def test_unavailable_selection_is_sanitized_and_never_falls_back(monkeypatch, failure):
    def validate(*args, **kwargs):
        raise failure("private account diagnostics")

    monkeypatch.setattr(execution_registry, "validate_selection", validate)
    with pytest.raises(broker.SettingsError) as error:
        broker.update_value(SETTING, scope="profile", value=CODEX, expected_revision="value:0")
    assert "private account" not in str(error.value)
    assert broker.get_dashboard_chat_execution_default() == SONNET


def test_reset_validates_the_frozen_default_without_replacing_an_unavailable_model(monkeypatch, validated):
    changed, _ = broker.update_value(SETTING, scope="profile", value=CODEX, expected_revision="value:0")

    def unavailable(selection, *, refresh):
        assert selection.model_id == "sonnet"
        assert refresh is True
        raise ProviderUnavailableError("account absent")

    monkeypatch.setattr(execution_registry, "validate_selection", unavailable)
    with pytest.raises(broker.SettingsError, match="not available"):
        broker.reset_value(SETTING, scope="profile", expected_revision=changed["revision"])
    assert value()["effective_value"] == CODEX
    assert value()["revision"] == changed["revision"]


def test_read_only_and_stale_writes_do_not_probe(validated):
    with pytest.raises(broker.SettingsError) as read_only:
        broker.update_value(SETTING, scope="profile", value=CODEX, expected_revision="value:0", read_only=True)
    assert read_only.value.code == "read_only"
    with pytest.raises(broker.SettingsError) as stale:
        broker.update_value(SETTING, scope="profile", value=CODEX, expected_revision="value:99")
    assert stale.value.code == "revision_conflict"
    assert stale.value.value["effective_value"] == SONNET
    with pytest.raises(broker.SettingsError) as read_only_reset:
        broker.reset_value(SETTING, scope="profile", expected_revision="value:0", read_only=True)
    assert read_only_reset.value.code == "read_only"
    assert validated == []


def test_cached_global_registry_tracks_settings_default_without_probing(monkeypatch, validated):
    instance = execution_registry.get_registry()
    assert instance.default_selection.model_id == "sonnet"
    monkeypatch.setattr(instance.get_provider("codex"), "probe", lambda **kwargs: pytest.fail("default read probed a provider"))
    broker.update_value(SETTING, scope="profile", value=CODEX, expected_revision="value:0")
    assert execution_registry.get_registry() is instance
    assert execution_registry.default_selection() == AgentExecutionSelection("codex", CODEX["model_id"], "Codex", CODEX["model_id"])


def test_catalog_uses_lazy_default_without_reconstructing_registry():
    first = AgentExecutionSelection("fixture", "one", "Fixture", "One")
    second = AgentExecutionSelection("fixture", "two", "Fixture", "Two")
    current = first
    provider = SimpleNamespace(provider_id="fixture", probe=lambda **kwargs: "descriptor")
    instance = execution_registry.ProviderRegistry([provider], default_selection=first, default_resolver=lambda: current)
    assert instance.get_catalog().default_selection is first
    current = second
    assert instance.get_catalog().default_selection is second


def test_corrupt_default_fails_closed(monkeypatch):
    value()
    with store.get_connection() as conn:
        conn.execute("UPDATE setting_value_state SET active_value_json = ? WHERE setting_id = ?", ('{"provider_id":"codex"}', SETTING))
    with pytest.raises(broker.SettingsError) as error:
        execution_registry.default_selection()
    assert error.value.code == "execution_selection_corrupt"
