"""Tests for sidecar cron expression matching."""

import os
import sys
from datetime import datetime, timezone

# Ensure work_buddy is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("WORK_BUDDY_SESSION_ID", "test-sidecar")

from work_buddy.sidecar.scheduler.cron import (
    cron_matches,
    next_cron_match,
    parse_cron_field,
)


def test_parse_wildcard():
    assert parse_cron_field("*", 0, 59) == set(range(60))


def test_parse_step():
    assert parse_cron_field("*/15", 0, 59) == {0, 15, 30, 45}


def test_parse_range():
    assert parse_cron_field("1-5", 1, 31) == {1, 2, 3, 4, 5}


def test_parse_range_with_step():
    assert parse_cron_field("0-10/3", 0, 59) == {0, 3, 6, 9}


def test_parse_list():
    assert parse_cron_field("1,3,5", 0, 6) == {1, 3, 5}


def test_parse_mixed():
    result = parse_cron_field("1-3,7,10-12", 0, 59)
    assert result == {1, 2, 3, 7, 10, 11, 12}


def test_cron_matches_simple():
    # "every minute" should match any time
    dt = datetime(2026, 4, 6, 14, 30, 0, tzinfo=timezone.utc)
    assert cron_matches("* * * * *", dt)


def test_cron_matches_specific_time():
    # "at 14:30 every day"
    dt = datetime(2026, 4, 6, 14, 30, 0, tzinfo=timezone.utc)
    assert cron_matches("30 14 * * *", dt)
    # Wrong minute
    dt2 = datetime(2026, 4, 6, 14, 31, 0, tzinfo=timezone.utc)
    assert not cron_matches("30 14 * * *", dt2)


def test_cron_matches_weekday():
    # 2026-04-06 is a Monday. Cron DOW: 0=Sun, 1=Mon, ..., 6=Sat
    dt_mon = datetime(2026, 4, 6, 9, 0, 0, tzinfo=timezone.utc)
    assert cron_matches("0 9 * * 1", dt_mon)  # Monday
    assert not cron_matches("0 9 * * 0", dt_mon)  # Sunday


def test_cron_matches_weekday_range():
    # "weekdays at 9am" = 1-5 (Mon-Fri)
    dt_mon = datetime(2026, 4, 6, 9, 0, 0, tzinfo=timezone.utc)
    assert cron_matches("0 9 * * 1-5", dt_mon)

    # Saturday = DOW 6
    dt_sat = datetime(2026, 4, 11, 9, 0, 0, tzinfo=timezone.utc)
    assert not cron_matches("0 9 * * 1-5", dt_sat)


def test_cron_matches_month():
    dt_apr = datetime(2026, 4, 6, 9, 0, 0, tzinfo=timezone.utc)
    assert cron_matches("0 9 * 4 *", dt_apr)
    assert not cron_matches("0 9 * 5 *", dt_apr)


def test_next_cron_match():
    after = datetime(2026, 4, 6, 14, 28, 0, tzinfo=timezone.utc)
    nxt = next_cron_match("30 14 * * *", after)
    assert nxt is not None
    assert nxt.hour == 14
    assert nxt.minute == 30
    assert nxt.day == 6


def test_next_cron_match_rolls_over():
    # If we're past 14:30, next match is tomorrow
    after = datetime(2026, 4, 6, 14, 31, 0, tzinfo=timezone.utc)
    nxt = next_cron_match("30 14 * * *", after)
    assert nxt is not None
    assert nxt.day == 7


def test_next_cron_match_with_timezone():
    # 14:30 UTC = 10:30 ET (UTC-4 in April)
    after = datetime(2026, 4, 6, 14, 0, 0, tzinfo=timezone.utc)
    nxt = next_cron_match("30 10 * * *", after, timezone="America/New_York")
    assert nxt is not None
    # Should match 10:30 ET = 14:30 UTC
    assert nxt.minute == 30


if __name__ == "__main__":
    test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in test_funcs:
        try:
            fn()
            print(f"  PASS: {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed out of {passed + failed}")
    sys.exit(1 if failed else 0)
