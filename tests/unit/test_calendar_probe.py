"""Provider-aware `calendar` tool probe — dispatches on calendar.provider.

The novel bit vs every other probe: it reads config and checks *whichever*
backend is configured, with no static depends_on (the bridge's Obsidian
dependency is resolved inside the dispatch).
"""

from __future__ import annotations


def _set_provider(monkeypatch, provider, **calendar_extra):
    import work_buddy.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda: {"calendar": {"provider": provider, **calendar_extra}},
    )


def test_native_token_present_is_available(monkeypatch):
    from work_buddy import tools
    from work_buddy.calendar import google_auth

    _set_provider(monkeypatch, "google_native")
    monkeypatch.setattr(google_auth, "token_status", lambda cfg=None: {"token_present": True})
    ok, _reason = tools._probe_calendar()
    assert ok is True


def test_native_no_token_is_unavailable(monkeypatch):
    from work_buddy import tools
    from work_buddy.calendar import google_auth

    _set_provider(monkeypatch, "google_native")
    monkeypatch.setattr(google_auth, "token_status", lambda cfg=None: {"token_present": False})
    ok, reason = tools._probe_calendar()
    assert ok is False and "OAuth token" in reason


def test_fake_provider_always_available(monkeypatch):
    from work_buddy import tools

    _set_provider(monkeypatch, "fake")
    assert tools._probe_calendar() == (True, "")


def test_unknown_provider_unavailable(monkeypatch):
    from work_buddy import tools

    _set_provider(monkeypatch, "weather_app")
    ok, reason = tools._probe_calendar()
    assert ok is False and "Unknown" in reason


def test_bridge_reads_plugin_cache(monkeypatch):
    from work_buddy import tools

    _set_provider(monkeypatch, "obsidian_bridge")
    # Obsidian already probed; plugin present → available.
    monkeypatch.setattr(tools, "_OBSIDIAN_PLUGINS", {"google-calendar": True})
    assert tools._probe_calendar()[0] is True
    # Plugin absent → unavailable, with provider-specific reason.
    monkeypatch.setattr(tools, "_OBSIDIAN_PLUGINS", {"google-calendar": False})
    ok, reason = tools._probe_calendar()
    assert ok is False and "obsidian_bridge" in reason
