"""Timezone contract tests for the day-planner's current-time clamp."""

from datetime import datetime
from zoneinfo import ZoneInfo

from work_buddy import config
from work_buddy.obsidian.day_planner import planner


def test_current_local_minutes_uses_configured_work_buddy_timezone(monkeypatch):
    configured = ZoneInfo("Pacific/Kiritimati")
    observed: dict[str, object] = {}

    class _ObservedDatetime:
        @staticmethod
        def now(tz=None):
            observed["timezone"] = tz
            return datetime(2026, 7, 14, 13, 47, tzinfo=tz)

    monkeypatch.setattr(config, "_USER_TZ_CACHE", configured)
    monkeypatch.setattr(planner, "datetime", _ObservedDatetime)

    assert planner._current_local_minutes() == 13 * 60 + 47
    assert observed["timezone"] is configured
