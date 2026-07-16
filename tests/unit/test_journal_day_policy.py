from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from work_buddy.journal_day import (
    InvalidLocalTime,
    day_for_instant,
    next_safe_boundary_transition,
    parse_local_time,
    resolve_local_datetime,
    window_for_local_date,
)


NY = ZoneInfo("America/New_York")


def test_local_time_schema_is_strict() -> None:
    assert parse_local_time("05:00").hour == 5
    for invalid in ("5:00", "05:0", "24:00", "05:60", "noon", ""):
        with pytest.raises(InvalidLocalTime):
            parse_local_time(invalid)


def test_day_identity_before_midnight_boundary_and_exact_boundary() -> None:
    assert day_for_instant(datetime(2026, 7, 15, 0, 0, tzinfo=NY), NY, "05:00") == date(2026, 7, 14)
    assert day_for_instant(datetime(2026, 7, 15, 4, 59, tzinfo=NY), NY, "05:00") == date(2026, 7, 14)
    assert day_for_instant(datetime(2026, 7, 15, 5, 0, tzinfo=NY), NY, "05:00") == date(2026, 7, 15)


def test_spring_forward_window_uses_next_civil_boundary_not_24_hours() -> None:
    window = window_for_local_date(date(2026, 3, 7), NY, "05:00")
    elapsed = window.end.astimezone(timezone.utc) - window.start.astimezone(timezone.utc)
    assert elapsed.total_seconds() == 23 * 60 * 60
    assert window.start.isoformat() == "2026-03-07T05:00:00-05:00"
    assert window.end.isoformat() == "2026-03-08T05:00:00-04:00"


def test_fall_back_window_is_25_elapsed_hours() -> None:
    window = window_for_local_date(date(2026, 10, 31), NY, "05:00")
    elapsed = window.end.astimezone(timezone.utc) - window.start.astimezone(timezone.utc)
    assert elapsed.total_seconds() == 25 * 60 * 60
    assert window.end.isoformat() == "2026-11-01T05:00:00-05:00"


def test_compatible_policy_shifts_gap_forward_and_chooses_earlier_fold() -> None:
    gap = resolve_local_datetime(date(2026, 3, 8), "02:30", NY)
    assert gap.isoformat() == "2026-03-08T03:30:00-04:00"

    fold = resolve_local_datetime(date(2026, 11, 1), "01:30", NY)
    assert fold.isoformat() == "2026-11-01T01:30:00-04:00"


def test_day_identity_compares_real_instants_inside_fall_back_fold() -> None:
    # Boundary 01:30 resolves to the earlier (-04:00) occurrence. The second
    # 01:15 (-05:00) is later on the timeline and therefore belongs to Nov 1.
    second_0115 = datetime(2026, 11, 1, 1, 15, tzinfo=NY, fold=1)
    assert second_0115.isoformat() == "2026-11-01T01:15:00-05:00"
    assert day_for_instant(second_0115, NY, "01:30") == date(2026, 11, 1)


def test_day_identity_respects_shifted_spring_forward_boundary() -> None:
    # Nonexistent 02:30 resolves compatibly to 03:30. A real 03:00 instant is
    # still before that boundary even though its wall time is numerically later.
    before_shifted_boundary = datetime(2026, 3, 8, 3, 0, tzinfo=NY)
    at_shifted_boundary = datetime(2026, 3, 8, 3, 30, tzinfo=NY)
    assert day_for_instant(before_shifted_boundary, NY, "02:30") == date(2026, 3, 7)
    assert day_for_instant(at_shifted_boundary, NY, "02:30") == date(2026, 3, 8)


def test_boundary_transition_waits_for_later_wall_time() -> None:
    observed = datetime(2026, 7, 15, 12, 0, tzinfo=NY)
    later = next_safe_boundary_transition(observed, NY, "05:00", "07:00")
    earlier = next_safe_boundary_transition(observed, NY, "05:00", "03:00")
    assert later.isoformat() == "2026-07-16T07:00:00-04:00"
    assert earlier.isoformat() == "2026-07-16T05:00:00-04:00"


def test_boundary_transition_uses_compatible_dst_resolution() -> None:
    spring = next_safe_boundary_transition(
        datetime(2026, 3, 7, 12, 0, tzinfo=NY),
        NY,
        "01:30",
        "02:30",
    )
    assert spring.isoformat() == "2026-03-08T03:30:00-04:00"

    fall = next_safe_boundary_transition(
        datetime(2026, 10, 31, 12, 0, tzinfo=NY),
        NY,
        "00:30",
        "01:30",
    )
    assert fall.isoformat() == "2026-11-01T01:30:00-04:00"


def test_policy_rejects_naive_instants() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        day_for_instant(datetime(2026, 7, 15, 5, 0), NY, "05:00")
