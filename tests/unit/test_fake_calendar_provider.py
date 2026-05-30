"""Tests for the in-memory FakeCalendarProvider."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from work_buddy.calendar.errors import CalendarEventNotFound
from work_buddy.calendar.identity import stable_key_for
from work_buddy.calendar.models import CalendarEvent, EventTime
from work_buddy.calendar.providers.fake import FakeCalendarProvider


def _timed_event(cal, eid, day, hh, *, ical_uid="", summary="x"):
    edt = timezone(timedelta(hours=-4))
    return CalendarEvent(
        stable_key=stable_key_for(ical_uid=ical_uid, provider="fake",
                                  calendar_id=cal, provider_event_id=eid),
        provider="fake", calendar_id=cal, provider_event_id=eid, summary=summary,
        start=EventTime(dt=datetime(2026, 4, day, hh, 0, tzinfo=edt)),
        end=EventTime(dt=datetime(2026, 4, day, hh + 1, 0, tzinfo=edt)),
        ical_uid=ical_uid,
    )


@pytest.fixture
def prov():
    p = FakeCalendarProvider()
    p.add_calendar("primary", "Me", is_primary=True)
    p.add_calendar("sk", "SickKids", access_role="reader", is_shared=True)
    return p


def test_list_calendars(prov):
    cals = prov.list_calendars()
    assert {c.id for c in cals} == {"primary", "sk"}


def test_list_events_window_and_filter(prov):
    prov.add_events([
        _timed_event("primary", "e1", 4, 9),
        _timed_event("sk", "e2", 5, 14),
        _timed_event("primary", "e3", 20, 9),   # outside window
    ])
    evs = prov.list_events(start="2026-04-01", end="2026-04-07")
    assert {e.provider_event_id for e in evs} == {"e1", "e2"}
    # sorted by start
    assert [e.provider_event_id for e in evs] == ["e1", "e2"]
    only_sk = prov.list_events(start="2026-04-01", end="2026-04-07", calendar_ids=["sk"])
    assert {e.provider_event_id for e in only_sk} == {"e2"}


def test_blacklist_hides_calendar(prov):
    prov.add_event(_timed_event("sk", "e2", 5, 14))
    prov.blacklist("sk")
    assert prov.list_events(start="2026-04-01", end="2026-04-07") == []
    assert prov.blacklisted_calendar_ids() == ["sk"]


def test_same_ical_uid_across_calendars(prov):
    prov.add_events([
        _timed_event("primary", "rowA", 4, 9, ical_uid="meet@g"),
        _timed_event("sk", "rowB", 4, 9, ical_uid="meet@g"),
    ])
    evs = prov.list_events(start="2026-04-01", end="2026-04-07")
    assert len({e.stable_key for e in evs}) == 1     # dedups to one key
    assert len(evs) == 2                              # but two rows present


def test_get_event_and_missing(prov):
    prov.add_event(_timed_event("primary", "e1", 4, 9))
    assert prov.get_event(calendar_id="primary", event_id="e1").provider_event_id == "e1"
    with pytest.raises(CalendarEventNotFound):
        prov.get_event(calendar_id="primary", event_id="zzz")


def test_write_methods_mutate_state(prov):
    res = prov.create_event(
        summary="New", start="2026-04-10T09:00:00", end="2026-04-10T10:00:00",
        calendar_id="primary",
    )
    assert res["success"]
    eid = res["id"]
    ev = prov.get_event(calendar_id="primary", event_id=eid)
    assert ev.summary == "New" and ev.wb_origin

    prov.update_event(calendar_id="primary", event_id=eid, changes={"summary": "Renamed"})
    assert prov.get_event(calendar_id="primary", event_id=eid).summary == "Renamed"

    prov.delete_event(calendar_id="primary", event_id=eid)
    with pytest.raises(CalendarEventNotFound):
        prov.get_event(calendar_id="primary", event_id=eid)
