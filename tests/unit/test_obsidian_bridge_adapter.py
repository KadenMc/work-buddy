"""Mapping-correctness tests for the Obsidian-bridge calendar adapter.

Drives a stubbed ``env`` so we exercise the dict→model mapping without Obsidian:
all-day vs timed, offset preservation, is_shared heuristic, the loc: stable key
(no iCalUID from the bridge), calendar_ids filtering, get_event-via-window, and
RuntimeError→typed-CalendarError translation.
"""

from __future__ import annotations

from datetime import timedelta, timezone

import pytest

from work_buddy.calendar.errors import (
    CalendarBridgeUnreachable,
    CalendarEventNotFound,
    CalendarProviderError,
)
from work_buddy.calendar.providers import obsidian_bridge as ob


@pytest.fixture
def adapter():
    return ob.ObsidianBridgeCalendarProvider()


# --- list_calendars --------------------------------------------------------


def test_list_calendars_maps_fields_and_is_shared(monkeypatch, adapter):
    monkeypatch.setattr(ob.env, "get_calendars", lambda: [
        {"id": "primary", "summary": "Me", "summaryOverride": None,
         "primary": True, "accessRole": "owner", "backgroundColor": "#111"},
        {"id": "sk", "summary": "SickKids", "summaryOverride": "SK",
         "primary": False, "accessRole": "reader", "backgroundColor": "#222"},
        {"id": "proj", "summary": "Project", "summaryOverride": None,
         "primary": False, "accessRole": "owner", "backgroundColor": "#333"},
    ])
    refs = adapter.list_calendars()
    primary, sk, proj = refs
    assert primary.is_primary and not primary.is_shared
    assert sk.name == "SK"               # summaryOverride wins
    assert sk.is_shared                  # non-primary + reader → shared
    assert not proj.is_shared            # non-primary owner → NOT shared (heuristic)
    assert primary.color == "#111"
    assert all(r.provider == "obsidian_bridge" for r in refs)


def test_blacklist_from_selected_flag(monkeypatch, adapter):
    monkeypatch.setattr(ob.env, "get_calendars", lambda: [
        {"id": "a", "selected": True},
        {"id": "b", "selected": False},
        {"id": "c"},  # missing selected → not blacklisted
    ])
    assert adapter.blacklisted_calendar_ids() == ["b"]


# --- list_events -----------------------------------------------------------


def _events_payload():
    return {"count": 3, "events": [
        {"id": "e1", "summary": "Standup", "status": "confirmed",
         "start": {"dateTime": "2026-04-04T09:30:00-04:00", "timeZone": "America/Toronto"},
         "end": {"dateTime": "2026-04-04T10:00:00-04:00", "timeZone": "America/Toronto"},
         "location": "Room 3", "description": "daily", "htmlLink": "http://x/e1",
         "calendarId": "primary", "calendarName": "Me", "transparency": "opaque"},
        {"id": "e2", "summary": "Holiday", "status": "confirmed",
         "start": {"date": "2026-04-05"}, "end": {"date": "2026-04-06"},
         "calendarId": "sk", "calendarName": "SickKids"},
        {"id": "e3", "summary": "Other", "status": "confirmed",
         "start": {"dateTime": "2026-04-04T14:00:00-04:00", "timeZone": "America/Toronto"},
         "end": {"dateTime": "2026-04-04T15:00:00-04:00", "timeZone": "America/Toronto"},
         "calendarId": "sk", "calendarName": "SickKids"},
    ]}


def test_list_events_maps_timed_and_all_day(monkeypatch, adapter):
    monkeypatch.setattr(ob.env, "get_events", lambda s, e: _events_payload())
    evs = adapter.list_events(start="2026-04-04", end="2026-04-06")
    e1 = next(e for e in evs if e.provider_event_id == "e1")
    e2 = next(e for e in evs if e.provider_event_id == "e2")
    # timed: offset preserved, not converted
    assert not e1.is_all_day
    assert e1.start.dt.utcoffset() == timedelta(hours=-4)
    assert e1.start.hhmm == "09:30"
    assert e1.start.tz_name == "America/Toronto"
    assert e1.location == "Room 3"
    assert e1.transparency == "opaque"
    # all-day: tz-free
    assert e2.is_all_day
    assert e2.start.date == "2026-04-05"
    # no iCalUID from the bridge → loc: stable key
    assert e1.stable_key == "loc:obsidian_bridge:primary:e1"


def test_list_events_calendar_ids_filter(monkeypatch, adapter):
    monkeypatch.setattr(ob.env, "get_events", lambda s, e: _events_payload())
    evs = adapter.list_events(start="2026-04-04", end="2026-04-06", calendar_ids=["sk"])
    assert {e.provider_event_id for e in evs} == {"e2", "e3"}


# --- get_event (via windowed list, since plugin getEvent is broken) --------


def test_get_event_filters_window(monkeypatch, adapter):
    monkeypatch.setattr(ob.env, "get_events", lambda s, e: _events_payload())
    ev = adapter.get_event(calendar_id="sk", event_id="e3")
    assert ev.summary == "Other"


def test_get_event_missing_raises(monkeypatch, adapter):
    monkeypatch.setattr(ob.env, "get_events", lambda s, e: _events_payload())
    with pytest.raises(CalendarEventNotFound):
        adapter.get_event(calendar_id="sk", event_id="nope")


# --- error mapping ---------------------------------------------------------


def test_bridge_unreachable_maps(monkeypatch, adapter):
    def boom():
        raise RuntimeError("Obsidian is not running. Please open Obsidian.")
    monkeypatch.setattr(ob.env, "get_calendars", boom)
    with pytest.raises(CalendarBridgeUnreachable):
        adapter.list_calendars()


def test_plugin_eval_error_maps_to_provider_error(monkeypatch, adapter):
    def boom(s, e):
        raise RuntimeError("Google Calendar error: getEvents failed: boom")
    monkeypatch.setattr(ob.env, "get_events", boom)
    with pytest.raises(CalendarProviderError):
        adapter.list_events(start="2026-04-04", end="2026-04-06")
