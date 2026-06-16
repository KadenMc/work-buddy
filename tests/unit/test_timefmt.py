"""Unit tests for the shared local-time formatting helpers.

The bundle collectors all render timestamps through ``work_buddy.timefmt`` so the
journal agent reads one local-time timeline. These tests pin a non-UTC zone
(America/Toronto = UTC-4 in June) so a missing UTC→local conversion fails loudly
rather than passing by coincidence under a UTC test host.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import work_buddy.config as config
from work_buddy import timefmt


@pytest.fixture
def pin_tz(monkeypatch):
    """Pin USER_TZ to America/Toronto by defeating the lazy cache.

    Monkeypatching the cached value short-circuits ``_compute_user_tz`` (no
    config load) and pins every call-time ``config.USER_TZ`` reader. The
    fixture auto-restores the original cache.
    """
    monkeypatch.setattr(config, "_USER_TZ_CACHE", ZoneInfo("America/Toronto"))
    return ZoneInfo("America/Toronto")


# ---------------------------------------------------------------------------
# parse_iso
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_suffix():
    assert timefmt.parse_iso("2026-06-05T17:30:00Z") == datetime(
        2026, 6, 5, 17, 30, tzinfo=timezone.utc
    )


def test_parse_iso_handles_explicit_offset():
    dt = timefmt.parse_iso("2026-06-05T17:30:00-04:00")
    assert dt.utcoffset().total_seconds() == -4 * 3600


def test_parse_iso_passes_datetime_through():
    dt = datetime(2026, 6, 5, 17, 30, tzinfo=timezone.utc)
    assert timefmt.parse_iso(dt) is dt


def test_parse_iso_none_and_garbage():
    assert timefmt.parse_iso(None) is None
    assert timefmt.parse_iso("") is None
    assert timefmt.parse_iso("not-a-date") is None


# ---------------------------------------------------------------------------
# to_local_naive
# ---------------------------------------------------------------------------


def test_to_local_naive_converts_aware(pin_tz):
    out = timefmt.to_local_naive(datetime(2026, 6, 5, 17, 30, tzinfo=timezone.utc))
    assert out == datetime(2026, 6, 5, 13, 30)  # UTC-4
    assert out.tzinfo is None


def test_to_local_naive_assumes_utc_for_naive(pin_tz):
    # A naive input is treated as UTC, making the helper total.
    out = timefmt.to_local_naive(datetime(2026, 6, 5, 17, 30))
    assert out == datetime(2026, 6, 5, 13, 30)


def test_to_local_naive_none_passes_through():
    assert timefmt.to_local_naive(None) is None


# ---------------------------------------------------------------------------
# format_local
# ---------------------------------------------------------------------------


def test_format_local_from_string(pin_tz):
    assert timefmt.format_local("2026-06-05T17:30:00Z", "%H:%M") == "13:30"


def test_format_local_from_datetime(pin_tz):
    dt = datetime(2026, 6, 5, 17, 30, tzinfo=timezone.utc)
    assert timefmt.format_local(dt, "%Y-%m-%d %H:%M") == "2026-06-05 13:30"


def test_format_local_fallback_on_unparseable(pin_tz):
    assert timefmt.format_local(None, "%H:%M", fallback="??:??") == "??:??"
    assert timefmt.format_local("garbage", "%H:%M", fallback="??:??") == "??:??"


# ---------------------------------------------------------------------------
# format_session_span
# ---------------------------------------------------------------------------


def test_format_session_span_same_day(pin_tz):
    out = timefmt.format_session_span("2026-06-05T17:30:00Z", "2026-06-05T18:11:00Z")
    assert out == "2026-06-05 13:30–14:11"


def test_format_session_span_cross_day_local_boundary(pin_tz):
    # 23:30Z → 19:30 local, 01:11Z(next) → 21:11 local — same local day.
    out = timefmt.format_session_span("2026-06-05T23:30:00Z", "2026-06-06T01:11:00Z")
    assert out == "2026-06-05 19:30–21:11"


def test_format_session_span_cross_day_after_conversion(pin_tz):
    # 02:00Z → previous local day; spans two local dates.
    out = timefmt.format_session_span("2026-06-06T02:00:00Z", "2026-06-06T05:00:00Z")
    assert out == "2026-06-05 22:00–2026-06-06 01:00"


def test_format_session_span_one_sided(pin_tz):
    assert (
        timefmt.format_session_span("2026-06-05T17:30:00Z", None)
        == "2026-06-05 13:30"
    )
    assert (
        timefmt.format_session_span(None, "2026-06-05T18:11:00Z")
        == "2026-06-05 14:11"
    )


def test_format_session_span_neither_uses_fallback_then_empty():
    assert timefmt.format_session_span(None, None, fallback="x") == "x"
    assert timefmt.format_session_span(None, None, empty="—") == "—"
    # fallback wins over empty when both supplied.
    assert timefmt.format_session_span(None, None, fallback="x", empty="—") == "x"
