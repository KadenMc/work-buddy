"""Tests for the calendar collector.

R0 (this file's first section) pins the timezone-correctness fix: "today" must
resolve in the user's configured zone (``config.USER_TZ``), not the process zone.
R6 (second section) drives ``collect()`` over the fake provider and checks the
rendered markdown stays byte-similar to the pre-cutover output.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time

from work_buddy import config
from work_buddy.collectors import calendar_collector as cc


# ---------------------------------------------------------------------------
# R0 — timezone-aware "today"
# ---------------------------------------------------------------------------


@freeze_time("2026-05-29T02:30:00Z")
def test_today_str_uses_user_tz_not_process_tz(monkeypatch):
    """At 02:30 UTC on the 29th it is still the 28th in Toronto (EDT, UTC-4).

    The naive ``datetime.now()`` this replaced would have reported the 29th
    (process/UTC zone). Reading ``config.USER_TZ`` makes the calendar day track
    the user, not the host clock.
    """
    monkeypatch.setattr(config, "USER_TZ", ZoneInfo("America/Toronto"), raising=False)
    assert cc._today_str() == "2026-05-28"

    monkeypatch.setattr(config, "USER_TZ", ZoneInfo("UTC"), raising=False)
    assert cc._today_str() == "2026-05-29"


@freeze_time("2026-05-29T02:30:00Z")
def test_resolve_date_range_defaults_to_user_tz_today(monkeypatch):
    monkeypatch.setattr(config, "USER_TZ", ZoneInfo("America/Toronto"), raising=False)
    start, end = cc._resolve_date_range({})
    assert start == end == "2026-05-28"


def test_resolve_date_range_honors_explicit_overrides(monkeypatch):
    monkeypatch.setattr(config, "USER_TZ", ZoneInfo("America/Toronto"), raising=False)
    start, end = cc._resolve_date_range(
        {"since": "2026-04-01T00:00:00", "until": "2026-04-05T00:00:00"}
    )
    assert start == "2026-04-01"
    assert end == "2026-04-05"
