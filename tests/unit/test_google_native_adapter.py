"""google_native adapter — mapping + the bespoke sync request helper.

Drives the adapter over ``httpx.MockTransport`` with injected fake credentials,
so no real Google or OAuth is touched. Covers JSON→model mapping, pagination,
401-refresh, 429/5xx backoff, and the critical 403-by-``reason`` disambiguation.
"""

from __future__ import annotations

from datetime import timedelta, timezone

import httpx
import pytest

from work_buddy.calendar.errors import (
    CalendarEventNotFound,
    CalendarProviderError,
    CalendarWriteUnsupported,
)
from work_buddy.calendar.provider import CalendarProvider
from work_buddy.calendar.providers import google_native as gn
from work_buddy.calendar.providers.google_native import GoogleNativeCalendarProvider

_EDT = timezone(timedelta(hours=-4))


class _FakeCreds:
    def __init__(self, token="tok"):
        self.token = token
        self.valid = True
        self.refresh_token = "rtok"
        self.refreshed = 0

    def refresh(self, _request):
        self.refreshed += 1
        self.token = "tok2"


def _provider(handler, creds=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GoogleNativeCalendarProvider({}, client=client, credentials=creds or _FakeCreds())


_CALS = {"items": [
    {"id": "primary@x", "summary": "Me", "primary": True, "accessRole": "owner",
     "backgroundColor": "#111", "timeZone": "America/Toronto"},
    {"id": "sk@import", "summary": "SickKids", "summaryOverride": "SK", "primary": False,
     "accessRole": "reader", "backgroundColor": "#222"},
]}

_TIMED = {
    "id": "e1", "iCalUID": "meet@google", "status": "confirmed", "summary": "Standup",
    "start": {"dateTime": "2026-06-01T09:00:00-04:00", "timeZone": "America/Toronto"},
    "end": {"dateTime": "2026-06-01T09:30:00-04:00", "timeZone": "America/Toronto"},
    "htmlLink": "http://x/e1", "transparency": "opaque",
    "extendedProperties": {"private": {"wb_origin": "1"}},
}
_ALLDAY = {
    "id": "e2", "iCalUID": "", "status": "confirmed", "summary": "Holiday",
    "start": {"date": "2026-06-01"}, "end": {"date": "2026-06-02"},
}


# --- discovery / mapping ----------------------------------------------------


def test_list_calendars_maps_fields():
    p = _provider(lambda req: httpx.Response(200, json=_CALS))
    refs = p.list_calendars()
    sk = next(r for r in refs if r.id == "sk@import")
    prim = next(r for r in refs if r.is_primary)
    assert sk.name == "SK" and sk.is_shared and sk.access_role == "reader"
    assert not prim.is_shared and prim.color == "#111"
    assert all(r.provider == "google_native" for r in refs)


def test_provider_satisfies_protocol():
    assert isinstance(_provider(lambda req: httpx.Response(200, json=_CALS)), CalendarProvider)


def test_list_events_maps_and_paginates(monkeypatch):
    from work_buddy import config
    monkeypatch.setattr(config, "USER_TZ", _EDT, raising=False)

    def handler(req):
        if req.url.path.endswith("/calendarList"):
            return httpx.Response(200, json={"items": [
                {"id": "primary@x", "summary": "Me", "primary": True, "accessRole": "owner"},
            ]})
        if "/events" in req.url.path:
            if not req.url.params.get("pageToken"):
                return httpx.Response(200, json={"items": [_TIMED], "nextPageToken": "p2"})
            return httpx.Response(200, json={"items": [_ALLDAY]})
        return httpx.Response(404)

    evs = _provider(handler).list_events(start="2026-06-01", end="2026-06-02")
    assert len(evs) == 2
    timed = next(e for e in evs if e.provider_event_id == "e1")
    assert not timed.is_all_day
    assert timed.start.dt.utcoffset() == timedelta(hours=-4)   # offset preserved
    assert timed.ical_uid == "meet@google"
    assert timed.stable_key == "ical:meet@google"              # real cross-calendar key
    assert timed.wb_origin is True
    assert timed.transparency == "opaque"
    allday = next(e for e in evs if e.provider_event_id == "e2")
    assert allday.is_all_day and allday.start.date == "2026-06-01"


def test_list_events_skips_cancelled(monkeypatch):
    from work_buddy import config
    monkeypatch.setattr(config, "USER_TZ", _EDT, raising=False)
    cancelled = {**_TIMED, "id": "e9", "status": "cancelled"}

    def handler(req):
        if req.url.path.endswith("/calendarList"):
            return httpx.Response(200, json={"items": [{"id": "primary@x", "primary": True, "accessRole": "owner"}]})
        return httpx.Response(200, json={"items": [_TIMED, cancelled]})

    evs = _provider(handler).list_events(start="2026-06-01", end="2026-06-02")
    assert {e.provider_event_id for e in evs} == {"e1"}


# --- request helper: refresh / backoff / 403 disambiguation -----------------


def test_refreshes_on_401():
    creds = _FakeCreds()
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(401, json={"error": {"message": "invalid"}})
        return httpx.Response(200, json=_CALS)

    refs = _provider(handler, creds=creds).list_calendars()
    assert creds.refreshed == 1 and creds.token == "tok2" and len(refs) == 2


def test_retries_on_429(monkeypatch):
    monkeypatch.setattr(gn, "_sleep", lambda *_: None)
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
                                  headers={"Retry-After": "0"})
        return httpx.Response(200, json=_CALS)

    assert len(_provider(handler).list_calendars()) == 2 and state["n"] == 2


def test_403_rate_limit_retries(monkeypatch):
    monkeypatch.setattr(gn, "_sleep", lambda *_: None)
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})
        return httpx.Response(200, json=_CALS)

    assert len(_provider(handler).list_calendars()) == 2 and state["n"] == 2


def test_403_forbidden_is_terminal(monkeypatch):
    monkeypatch.setattr(gn, "_sleep", lambda *_: None)
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        return httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}], "message": "insufficient scope"}})

    with pytest.raises(CalendarProviderError):
        _provider(handler).list_calendars()
    assert state["n"] == 1   # terminal — no retry


def test_get_event_404_maps(monkeypatch):
    p = _provider(lambda req: httpx.Response(404, json={"error": {"message": "not found"}}))
    with pytest.raises(CalendarEventNotFound):
        p.get_event(calendar_id="primary@x", event_id="missing")


# --- health -----------------------------------------------------------------


def test_health_ready_and_unready():
    ready = _provider(lambda req: httpx.Response(200, json=_CALS)).health()
    assert ready["ready"] and ready["calendar_count"] == 2
    unready = _provider(
        lambda req: httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}})
    ).health()
    assert unready["ready"] is False and "reason" in unready


# --- writes are not yet supported -------------------------------------------


def test_writes_raise_unsupported():
    p = _provider(lambda req: httpx.Response(200, json=_CALS))
    for fn in (lambda: p.create_event(summary="x", start="a", end="b"),
               lambda: p.update_event(calendar_id="c", event_id="e", changes={}),
               lambda: p.delete_event(calendar_id="c", event_id="e")):
        with pytest.raises(CalendarWriteUnsupported):
            fn()


# --- factory ----------------------------------------------------------------


def test_factory_selects_google_native(monkeypatch):
    import work_buddy.config as cfgmod
    from work_buddy.calendar import provider as pm

    monkeypatch.setattr(cfgmod, "load_config", lambda: {"calendar": {"provider": "google_native"}})
    assert isinstance(pm.get_calendar_provider(), GoogleNativeCalendarProvider)


# --- auth module (no live Google) -------------------------------------------


def test_auth_missing_token_disabled(monkeypatch, tmp_path):
    from work_buddy.calendar import google_auth as ga
    from work_buddy.calendar.errors import CalendarProviderDisabled

    monkeypatch.setattr(ga, "_token_path", lambda: tmp_path / "token.json")
    with pytest.raises(CalendarProviderDisabled):
        ga.load_credentials({})


def test_auth_token_status(monkeypatch, tmp_path):
    from work_buddy.calendar import google_auth as ga

    tp = tmp_path / "token.json"
    monkeypatch.setattr(ga, "_token_path", lambda: tp)
    assert ga.token_status({})["token_present"] is False
    tp.write_text("{}", encoding="utf-8")
    assert ga.token_status({})["token_present"] is True
