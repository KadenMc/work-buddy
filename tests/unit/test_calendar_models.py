"""Roundtrip + invariant tests for the canonical calendar models.

The load-bearing invariant: serialization preserves the provider's offset
verbatim (no ``.astimezone()``), and all-day events stay timezone-free.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from work_buddy.calendar.models import (
    CalendarEvent,
    CalendarRef,
    CoverageReport,
    EventTime,
)


def test_event_time_timed_roundtrip_preserves_offset():
    edt = timezone(timedelta(hours=-4))  # America/Toronto in May (EDT)
    et = EventTime(dt=datetime(2026, 4, 4, 9, 30, tzinfo=edt), tz_name="America/Toronto")
    back = EventTime.from_dict(et.to_dict())
    assert back.dt == et.dt
    assert back.dt.utcoffset() == timedelta(hours=-4)   # offset survives
    assert back.tz_name == "America/Toronto"
    assert not back.is_all_day


def test_event_time_all_day_is_tz_free():
    et = EventTime(date="2026-04-05")
    assert et.is_all_day
    assert et.dt is None
    back = EventTime.from_dict(et.to_dict())
    assert back.is_all_day
    assert back.date == "2026-04-05"
    assert back.tz_name is None


def test_event_time_display_helpers():
    edt = timezone(timedelta(hours=-4))
    timed = EventTime(dt=datetime(2026, 4, 4, 9, 30, tzinfo=edt))
    assert timed.date_key == "2026-04-04"
    assert timed.hhmm == "09:30"            # wall-clock, not converted
    allday = EventTime(date="2026-04-05")
    assert allday.date_key == "2026-04-05"
    assert allday.hhmm is None
    # all-day sorts before timed on the same day (bare date < ISO datetime)
    assert allday.sort_value < EventTime(
        dt=datetime(2026, 4, 5, 0, 0, tzinfo=edt)
    ).sort_value


def test_calendar_event_roundtrip():
    edt = timezone(timedelta(hours=-4))
    ev = CalendarEvent(
        stable_key="loc:obsidian_bridge:cal1:evt1",
        provider="obsidian_bridge",
        calendar_id="cal1",
        provider_event_id="evt1",
        summary="Standup",
        start=EventTime(dt=datetime(2026, 4, 4, 9, 30, tzinfo=edt), tz_name="America/Toronto"),
        end=EventTime(dt=datetime(2026, 4, 4, 10, 0, tzinfo=edt), tz_name="America/Toronto"),
        location="Room 3",
        calendar_name="Work",
        transparency="opaque",
    )
    back = CalendarEvent.from_dict(ev.to_dict())
    assert back == ev
    assert not back.is_all_day
    assert back.start.dt.utcoffset() == timedelta(hours=-4)


def test_calendar_ref_roundtrip():
    ref = CalendarRef(
        id="cal1", name="SickKids", provider="obsidian_bridge",
        is_primary=False, access_role="reader", is_shared=True, color="#aabbcc",
    )
    assert CalendarRef.from_dict(ref.to_dict()) == ref


def test_coverage_report_to_dict():
    rep = CoverageReport(
        window={"start": "2026-04-01", "end": "2026-04-07"},
        subscribed=[CalendarRef(id="c1", name="Primary", provider="obsidian_bridge", is_primary=True)],
        blacklisted=["c9"],
        per_calendar_counts={"c1": 12},
        errored={"c2": "fetch timeout"},
        total_events=12,
    )
    d = rep.to_dict()
    assert d["window"] == {"start": "2026-04-01", "end": "2026-04-07"}
    assert d["subscribed"][0]["name"] == "Primary"
    assert d["blacklisted"] == ["c9"]
    assert d["per_calendar_counts"] == {"c1": 12}
    assert d["errored"] == {"c2": "fetch timeout"}
    assert d["total_events"] == 12
