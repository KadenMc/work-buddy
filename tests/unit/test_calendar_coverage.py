"""Tests for the read-only calendar capabilities and coverage report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from work_buddy.calendar import capabilities as caps
from work_buddy.calendar.errors import CalendarBridgeUnreachable
from work_buddy.calendar.identity import stable_key_for
from work_buddy.calendar.models import CalendarEvent, EventTime
from work_buddy.calendar.providers.fake import FakeCalendarProvider

_EDT = timezone(timedelta(hours=-4))


def _ev(cal, eid, day):
    return CalendarEvent(
        stable_key=stable_key_for(ical_uid="", provider="fake", calendar_id=cal, provider_event_id=eid),
        provider="fake", calendar_id=cal, provider_event_id=eid, summary=eid,
        start=EventTime(dt=datetime(2026, 4, day, 9, 0, tzinfo=_EDT)),
        end=EventTime(dt=datetime(2026, 4, day, 10, 0, tzinfo=_EDT)),
    )


@pytest.fixture
def prov():
    p = FakeCalendarProvider()
    p.add_calendar("primary", "Me", is_primary=True)
    p.add_calendar("sk", "SickKids", access_role="reader", is_shared=True)
    p.add_calendar("empty", "Empty")
    p.add_events([_ev("primary", "p1", 4), _ev("primary", "p2", 5), _ev("sk", "s1", 4)])
    return p


def _use(monkeypatch, prov):
    monkeypatch.setattr(caps, "get_calendar_provider", lambda: prov)


def test_health_ok(monkeypatch, prov):
    _use(monkeypatch, prov)
    res = caps.calendar_health()
    assert res["ok"] and res["provider"] == "fake"
    assert res["ready"] is True


def test_health_propagates_typed_error(monkeypatch):
    class Boom(FakeCalendarProvider):
        def health(self):
            raise CalendarBridgeUnreachable("Obsidian not running")
    _use(monkeypatch, Boom())
    res = caps.calendar_health()
    assert not res["ok"]
    assert res["error_kind"] == "calendar_bridge_unreachable"


def test_list_events(monkeypatch, prov):
    _use(monkeypatch, prov)
    res = caps.list_calendar_events(start="2026-04-01", end="2026-04-07")
    assert res["ok"]
    assert res["count"] == 3
    assert res["window"] == {"start": "2026-04-01", "end": "2026-04-07"}
    # canonical serialized shape
    assert all("stable_key" in e for e in res["events"])


def test_list_events_requires_window(monkeypatch, prov):
    _use(monkeypatch, prov)
    res = caps.list_calendar_events(start="", end="")
    assert not res["ok"] and res["error_kind"] == "bad_request"


def test_get_event_found_and_missing(monkeypatch, prov):
    _use(monkeypatch, prov)
    ok = caps.get_calendar_event(calendar_id="primary", event_id="p1")
    assert ok["ok"] and ok["event"]["provider_event_id"] == "p1"
    missing = caps.get_calendar_event(calendar_id="primary", event_id="zzz")
    assert not missing["ok"] and missing["error_kind"] == "calendar_event_not_found"


def test_coverage_counts_per_calendar(monkeypatch, prov):
    _use(monkeypatch, prov)
    res = caps.calendar_coverage(start="2026-04-01", end="2026-04-07")
    assert res["ok"]
    assert len(res["subscribed"]) == 3
    assert res["per_calendar_counts"] == {"primary": 2, "sk": 1, "empty": 0}
    assert res["total_events"] == 3
    assert res["errored"] == {}


def test_coverage_excludes_blacklisted(monkeypatch, prov):
    prov.blacklist("sk")
    _use(monkeypatch, prov)
    res = caps.calendar_coverage(start="2026-04-01", end="2026-04-07")
    assert "sk" not in res["per_calendar_counts"]
    assert res["blacklisted"] == ["sk"]
    assert res["total_events"] == 2


def test_coverage_default_window(monkeypatch, prov):
    from work_buddy import config
    monkeypatch.setattr(config, "USER_TZ", _EDT, raising=False)
    _use(monkeypatch, prov)
    res = caps.calendar_coverage()
    assert res["ok"]
    assert "start" in res["window"] and "end" in res["window"]


def test_coverage_total_failure_populates_errored(monkeypatch, prov):
    def boom(*a, **k):
        raise CalendarBridgeUnreachable("bridge down mid-window")
    monkeypatch.setattr(prov, "list_events", boom)
    _use(monkeypatch, prov)
    rep = caps.build_coverage_report(prov, "2026-04-01", "2026-04-07")
    # every visible calendar flagged errored; none silently zero
    assert set(rep.errored) == {"primary", "sk", "empty"}
    assert rep.total_events == 0
