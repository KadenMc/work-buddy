"""Tests for cross-calendar/-adapter stable keys."""

from __future__ import annotations

from work_buddy.calendar.identity import stable_key_for


def test_ical_uid_preferred_when_present():
    key = stable_key_for(
        ical_uid="abc123@google.com",
        provider="obsidian_bridge",
        calendar_id="cal1",
        provider_event_id="evt1",
    )
    assert key == "ical:abc123@google.com"


def test_ical_uid_whitespace_trimmed():
    assert stable_key_for(
        ical_uid="  abc123@google.com  ",
        provider="p", calendar_id="c", provider_event_id="e",
    ) == "ical:abc123@google.com"


def test_falls_back_to_loc_when_no_uid():
    for uid in (None, "", "   "):
        assert stable_key_for(
            ical_uid=uid,
            provider="obsidian_bridge",
            calendar_id="cal1",
            provider_event_id="evt1",
        ) == "loc:obsidian_bridge:cal1:evt1"


def test_same_meeting_two_calendars_dedup_by_ical_uid():
    """Same logical meeting, different rows → one stable key via iCalUID."""
    primary = stable_key_for(
        ical_uid="meet@google.com", provider="obsidian_bridge",
        calendar_id="primary", provider_event_id="row_A",
    )
    shared = stable_key_for(
        ical_uid="meet@google.com", provider="obsidian_bridge",
        calendar_id="sickkids", provider_event_id="row_B",
    )
    assert primary == shared  # dedups across calendars


def test_no_uid_keeps_rows_distinct():
    a = stable_key_for(ical_uid="", provider="p", calendar_id="c1", provider_event_id="e")
    b = stable_key_for(ical_uid="", provider="p", calendar_id="c2", provider_event_id="e")
    assert a != b  # without a UID, provider-local rows stay distinct
