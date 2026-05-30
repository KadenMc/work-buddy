"""Tests for ``work_buddy.calendar.env`` low-level eval_js wrappers.

Pins the timezone behavior of ``create_event``: when the caller omits
``timezone``, it must default to the user's configured zone (``config.USER_TZ``)
rather than a hardcoded ``"America/Toronto"`` — a layering violation regardless
of value.

We call ``create_event.__wrapped__`` (functools.wraps exposes the undecorated
function) to bypass the ``@requires_consent`` gate, and stub ``_run_js`` to
capture the placeholder substitutions instead of touching the Obsidian bridge.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from work_buddy import config
from work_buddy.calendar import env


@pytest.fixture
def captured_run_js(monkeypatch):
    captured: dict[str, dict] = {}

    def _fake_run_js(snippet_name, replacements=None, timeout=15):
        captured["snippet"] = snippet_name
        captured["replacements"] = replacements or {}
        return {"success": True}

    monkeypatch.setattr(env, "_run_js", _fake_run_js)
    return captured


def test_create_event_defaults_timezone_to_user_tz(monkeypatch, captured_run_js):
    monkeypatch.setattr(config, "USER_TZ", ZoneInfo("America/Toronto"), raising=False)
    env.create_event.__wrapped__(
        summary="Standup",
        start="2026-04-05T09:00:00",
        end="2026-04-05T09:30:00",
        calendar_id="primary",
    )
    assert captured_run_js["replacements"]["__TIMEZONE__"] == "America/Toronto"


def test_create_event_respects_configured_non_toronto_tz(monkeypatch, captured_run_js):
    monkeypatch.setattr(config, "USER_TZ", ZoneInfo("America/Vancouver"), raising=False)
    env.create_event.__wrapped__(
        summary="Standup",
        start="2026-04-05T09:00:00",
        end="2026-04-05T09:30:00",
        calendar_id="primary",
    )
    assert captured_run_js["replacements"]["__TIMEZONE__"] == "America/Vancouver"


def test_create_event_explicit_timezone_wins(monkeypatch, captured_run_js):
    monkeypatch.setattr(config, "USER_TZ", ZoneInfo("America/Toronto"), raising=False)
    env.create_event.__wrapped__(
        summary="Standup",
        start="2026-04-05T09:00:00",
        end="2026-04-05T09:30:00",
        calendar_id="primary",
        timezone="Europe/London",
    )
    assert captured_run_js["replacements"]["__TIMEZONE__"] == "Europe/London"
