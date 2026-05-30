"""Factory + protocol-conformance tests for the calendar provider seam."""

from __future__ import annotations

import pytest

from work_buddy.calendar import provider as provider_mod
from work_buddy.calendar.errors import CalendarProviderDisabled
from work_buddy.calendar.provider import CalendarProvider, get_calendar_provider
from work_buddy.calendar.providers.fake import FakeCalendarProvider
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


def test_factory_defaults_to_obsidian_bridge(monkeypatch):
    _patch_cfg(monkeypatch, {})
    prov = get_calendar_provider()
    assert isinstance(prov, ObsidianBridgeCalendarProvider)
    assert prov.name == "obsidian_bridge"


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
