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


# ---------------------------------------------------------------------------
# R6 — collector renders over the provider seam (driven by the fake)
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta

from work_buddy.calendar import provider as provider_mod
from work_buddy.calendar.models import CalendarEvent, EventTime
from work_buddy.calendar.providers.fake import FakeCalendarProvider

_EDT = ZoneInfo("America/Toronto")


def _timed(cal_name, cal_id, eid, hh, mm, dur_min, summary, *, location=""):
    start = datetime(2026, 4, 4, hh, mm, tzinfo=_EDT)
    end = start + timedelta(minutes=dur_min)
    return CalendarEvent(
        stable_key=f"loc:fake:{cal_id}:{eid}", provider="fake",
        calendar_id=cal_id, provider_event_id=eid, summary=summary,
        start=EventTime(dt=start), end=EventTime(dt=end),
        location=location, calendar_name=cal_name,
    )


def _all_day(cal_name, cal_id, eid, date, summary):
    return CalendarEvent(
        stable_key=f"loc:fake:{cal_id}:{eid}", provider="fake",
        calendar_id=cal_id, provider_event_id=eid, summary=summary,
        start=EventTime(date=date), end=EventTime(date=date),
        calendar_name=cal_name,
    )


def _use_fake(monkeypatch, prov):
    monkeypatch.setattr(provider_mod, "get_calendar_provider", lambda: prov)


@freeze_time("2026-04-04T17:30:00Z")  # 13:30 in Toronto (EDT)
def test_collect_today_schedule_classifies_now_past_upcoming(monkeypatch):
    monkeypatch.setattr(config, "USER_TZ", _EDT, raising=False)
    prov = FakeCalendarProvider()
    prov.add_calendar("work", "Work")
    prov.add_events([
        _all_day("Personal", "personal", "h1", "2026-04-04", "Holiday"),
        _timed("Work", "work", "e1", 9, 30, 30, "Standup"),   # past
        _timed("Work", "work", "e2", 13, 0, 60, "Lunch"),     # current
        _timed("Work", "work", "e3", 15, 0, 60, "Review"),    # upcoming
    ])
    _use_fake(monkeypatch, prov)

    out = cc.collect({})
    assert out.startswith("# Calendar — Today's Schedule")
    assert "**Date:** 2026-04-04  " in out
    assert "**Current time:** 13:30  " in out
    assert "**Events:** 4 total (1 all-day, 3 timed)" in out
    assert "**Now:** 1 event(s) in progress" in out
    assert "**Upcoming:** 1 remaining today" in out
    assert "## All-Day Events" in out
    assert "- Holiday `Personal`" in out
    assert "- ~~09:30–10:00 — Standup~~ `Work`" in out
    assert "- 13:00–14:00 — Lunch `Work` **<-- NOW**" in out
    assert "- 15:00–16:00 — Review `Work`" in out


@freeze_time("2026-04-04T17:30:00Z")
def test_collect_today_empty(monkeypatch):
    monkeypatch.setattr(config, "USER_TZ", _EDT, raising=False)
    prov = FakeCalendarProvider()
    _use_fake(monkeypatch, prov)
    out = cc.collect({})
    assert "No events scheduled today." in out


def test_collect_range_groups_by_day(monkeypatch):
    monkeypatch.setattr(config, "USER_TZ", _EDT, raising=False)
    prov = FakeCalendarProvider()
    prov.add_calendar("work", "Work")
    prov.add_events([
        _timed("Work", "work", "e1", 9, 0, 60, "Mon AM"),
        _all_day("Personal", "personal", "h1", "2026-04-05", "Trip"),
    ])
    prov.add_event(CalendarEvent(
        stable_key="loc:fake:work:e2", provider="fake", calendar_id="work",
        provider_event_id="e2", summary="Tue PM",
        start=EventTime(dt=datetime(2026, 4, 5, 14, 0, tzinfo=_EDT)),
        end=EventTime(dt=datetime(2026, 4, 5, 15, 0, tzinfo=_EDT)),
        calendar_name="Work",
    ))
    _use_fake(monkeypatch, prov)

    out = cc.collect({"since": "2026-04-04T00:00:00", "until": "2026-04-06T00:00:00"})
    assert out.startswith("# Calendar — 2026-04-04 to 2026-04-06")
    assert "**Events:** 3" in out
    assert "## 2026-04-04" in out
    assert "- 09:00–10:00 — Mon AM `Work`" in out
    assert "## 2026-04-05" in out
    assert "- (all day) Trip `Personal`" in out
    assert "- 14:00–15:00 — Tue PM `Work`" in out


def test_collect_unavailable_when_provider_not_ready(monkeypatch):
    monkeypatch.setattr(config, "USER_TZ", _EDT, raising=False)
    prov = FakeCalendarProvider()
    prov.set_healthy(False)
    _use_fake(monkeypatch, prov)
    out = cc.collect({})
    assert "Calendar data not available" in out


def test_collect_unavailable_when_provider_disabled(monkeypatch):
    from work_buddy.calendar.errors import CalendarProviderDisabled

    def _raise():
        raise CalendarProviderDisabled("calendar.enabled is False in config")

    monkeypatch.setattr(provider_mod, "get_calendar_provider", _raise)
    out = cc.collect({})
    assert "Calendar data not available" in out
