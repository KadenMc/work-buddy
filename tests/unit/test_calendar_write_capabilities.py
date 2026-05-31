"""Heavy-consent calendar write capabilities.

Exercises the four heavy-consent properties without a live prompt: per-write
``consent_weight="high"``, the change-specific prompt body (rendered before the
gate raises), ``default_ttl=0`` on the destructive ops, and the shared-calendar
escalation. Consent is isolated to a tmp DB via a fresh ConsentCache, mirroring
``test_consent_risk_reduction``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from work_buddy.calendar import capabilities as caps
from work_buddy.calendar.errors import CalendarError, CalendarWriteUnsupported
from work_buddy.calendar.identity import stable_key_for
from work_buddy.calendar.models import CalendarEvent, EventTime
from work_buddy.calendar.providers.fake import FakeCalendarProvider

_EDT = timezone(timedelta(hours=-4))


def _fresh_cache(monkeypatch, tmp_path):
    """Swap the module ConsentCache for a tmp-backed one so no grant can leak
    into a real session DB. ``ConsentCache._get_db_path`` caches the resolved
    path, so we set ``_db_path`` directly — patching the ``agent_session``
    resolver is not enough (the cache reads its own cached binding)."""
    from work_buddy import consent as c

    cache = c.ConsentCache()
    cache._db_path = tmp_path / "consent.db"
    cache._initialized = True
    c._cache = cache
    return c


def _provider(monkeypatch, *, shared=False):
    from work_buddy import config
    monkeypatch.setattr(config, "USER_TZ", _EDT, raising=False)
    p = FakeCalendarProvider()
    p.add_calendar("primary", "Me", is_primary=True)
    p.add_calendar("sk", "SickKids", access_role="reader", is_shared=shared)
    monkeypatch.setattr(caps, "get_calendar_provider", lambda: p)
    return p


def _seed(p, cal, eid, summary="Standup"):
    p.add_event(CalendarEvent(
        stable_key=stable_key_for(ical_uid="", provider="fake", calendar_id=cal, provider_event_id=eid),
        provider="fake", calendar_id=cal, provider_event_id=eid, summary=summary,
        start=EventTime(dt=datetime(2026, 4, 4, 9, 30, tzinfo=_EDT)),
        end=EventTime(dt=datetime(2026, 4, 4, 10, 0, tzinfo=_EDT)),
        calendar_name="Me",
    ))


# --- the four-properties config is actually declared ------------------------


def test_write_consent_metadata_declares_heavy_properties():
    from work_buddy.consent import get_consent_metadata

    cm = get_consent_metadata("calendar.create_event")
    assert cm["consent_weight"] == "high"        # never carried by a workflow grant
    assert cm["default_ttl"] == 10               # create is reversible → small TTL
    assert cm["body_extras"] is not None         # dynamic prompt body wired
    for op in ("calendar.update_event", "calendar.delete_event"):
        m = get_consent_metadata(op)
        assert m["consent_weight"] == "high"
        assert m["default_ttl"] == 0             # destructive → always re-prompt
        assert m["body_extras"] is not None


# --- create -----------------------------------------------------------------


def test_create_without_grant_raises_and_renders_body(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)
    _provider(monkeypatch)
    with pytest.raises(c.ConsentRequired) as ei:
        caps.create_calendar_event(
            summary="Lunch", start="2026-04-04T12:00:00", end="2026-04-04T13:00:00",
            calendar_id="primary",
        )
    assert ei.value.operation == "calendar.create_event"
    body = caps._PENDING_BODY["calendar.create_event"]
    assert "Lunch" in body and "Me" in body
    assert "12:00" in body                       # rendered in USER_TZ
    assert caps._body_for("calendar.create_event")() == body


def test_create_with_grant_mutates_state(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)
    p = _provider(monkeypatch)
    c._cache.grant("calendar.create_event", "always")
    res = caps.create_calendar_event(
        summary="Lunch", start="2026-04-04T12:00:00", end="2026-04-04T13:00:00",
        calendar_id="primary",
    )
    assert res["ok"]
    evs = p.list_events(start="2026-04-01", end="2026-04-30")
    assert any(e.summary == "Lunch" for e in evs)


def test_create_shared_calendar_escalation(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)
    _provider(monkeypatch, shared=True)
    with pytest.raises(c.ConsentRequired):
        caps.create_calendar_event(
            summary="Clinic", start="2026-04-04T12:00:00", end="2026-04-04T13:00:00",
            calendar_id="sk",
        )
    assert "others can see this calendar" in caps._PENDING_BODY["calendar.create_event"]


# --- update -----------------------------------------------------------------


def test_update_renders_before_after_diff(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)
    p = _provider(monkeypatch)
    _seed(p, "primary", "e1", summary="Standup")
    with pytest.raises(c.ConsentRequired):
        caps.update_calendar_event(
            calendar_id="primary", event_id="e1", changes={"summary": "Daily Standup"},
        )
    body = caps._PENDING_BODY["calendar.update_event"]
    assert "Standup" in body and "Daily Standup" in body and "→" in body


def test_update_requires_non_empty_changes(monkeypatch, tmp_path):
    _fresh_cache(monkeypatch, tmp_path)
    _provider(monkeypatch)
    res = caps.update_calendar_event(calendar_id="primary", event_id="e1", changes={})
    assert not res["ok"] and res["error_kind"] == "bad_request"


# --- delete -----------------------------------------------------------------


def test_delete_without_grant_names_event(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)
    p = _provider(monkeypatch)
    _seed(p, "primary", "e1", summary="Standup")
    with pytest.raises(c.ConsentRequired):
        caps.delete_calendar_event(calendar_id="primary", event_id="e1")
    assert "Standup" in caps._PENDING_BODY["calendar.delete_event"]


def test_delete_with_grant_removes_event(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)
    p = _provider(monkeypatch)
    _seed(p, "primary", "e1")
    c._cache.grant("calendar.delete_event", "always")
    res = caps.delete_calendar_event(calendar_id="primary", event_id="e1")
    assert res["ok"]
    with pytest.raises(CalendarError):
        p.get_event(calendar_id="primary", event_id="e1")


# --- write-unsupported provider maps cleanly --------------------------------


def test_write_unsupported_maps_to_error(monkeypatch, tmp_path):
    c = _fresh_cache(monkeypatch, tmp_path)
    p = _provider(monkeypatch)

    def _unsupported(**kw):
        raise CalendarWriteUnsupported("read-only provider")

    monkeypatch.setattr(p, "create_event", _unsupported)
    c._cache.grant("calendar.create_event", "always")
    res = caps.create_calendar_event(
        summary="X", start="2026-04-04T12:00:00", end="2026-04-04T13:00:00",
        calendar_id="primary",
    )
    assert not res["ok"] and res["error_kind"] == "calendar_write_unsupported"
