"""Factory + protocol-conformance tests for the calendar provider seam."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from work_buddy.calendar import provider as provider_mod
from work_buddy.calendar.errors import CalendarProviderDisabled
from work_buddy.calendar.provider import CalendarProvider, get_calendar_provider
from work_buddy.calendar.providers.fake import FakeCalendarProvider
from work_buddy.calendar.providers.google_native import GoogleNativeCalendarProvider
from work_buddy.calendar.providers.obsidian_bridge import (
    ObsidianBridgeCalendarProvider,
)


def _patch_cfg(monkeypatch, calendar_cfg):
    monkeypatch.setattr(
        provider_mod, "load_config", lambda: {"calendar": calendar_cfg}, raising=False
    )
    # load_config is imported inside the function body, so patch the source too.
    import work_buddy.config as cfgmod
    monkeypatch.setattr(cfgmod, "load_config", lambda: {"calendar": calendar_cfg})


def test_factory_defaults_to_google_native(monkeypatch):
    _patch_cfg(monkeypatch, {})
    prov = get_calendar_provider()
    assert isinstance(prov, GoogleNativeCalendarProvider)
    assert prov.name == "google_native"


def test_shipped_sample_config_keeps_native_calendar_default():
    sample = Path(__file__).resolve().parents[2] / "config.example.yaml"
    cfg = yaml.safe_load(sample.read_text(encoding="utf-8"))

    assert cfg["calendar"]["provider"] == "google_native"


def test_factory_selects_fake(monkeypatch):
    _patch_cfg(monkeypatch, {"provider": "fake"})
    assert isinstance(get_calendar_provider(), FakeCalendarProvider)


def test_factory_disabled_raises(monkeypatch):
    _patch_cfg(monkeypatch, {"enabled": False})
    with pytest.raises(CalendarProviderDisabled):
        get_calendar_provider()


def test_factory_unknown_provider_raises(monkeypatch):
    _patch_cfg(monkeypatch, {"provider": "weather_app"})
    with pytest.raises(CalendarProviderDisabled):
        get_calendar_provider()


def test_both_providers_satisfy_protocol():
    # runtime_checkable Protocol — both concrete providers must structurally match.
    assert isinstance(FakeCalendarProvider(), CalendarProvider)
    assert isinstance(ObsidianBridgeCalendarProvider(), CalendarProvider)


def test_context_calendar_readiness_uses_selected_provider(monkeypatch):
    from work_buddy.mcp_server.context_wrappers import get_calendar_context

    fake = FakeCalendarProvider()
    monkeypatch.setattr(provider_mod, "get_calendar_provider", lambda: fake)
    result = json.loads(get_calendar_context(check_ready=True))
    assert result["provider"] == "fake"
    assert result["ready"] is True
