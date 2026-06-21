"""Unit tests for per-source fire-rate tracking (rolling-window count)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from work_buddy.events.sources.ratelimit import fires_last_hour, record_fire


def _t(h: int = 0, m: int = 0) -> datetime:
    return datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc) + timedelta(hours=h, minutes=m)


def test_record_and_count(tmp_path):
    assert fires_last_hour("s", _t(), tmp_path) == 0
    assert record_fire("s", _t(0, 0), tmp_path) == 1
    assert record_fire("s", _t(0, 10), tmp_path) == 2
    assert fires_last_hour("s", _t(0, 20), tmp_path) == 2


def test_window_prunes_old_fires(tmp_path):
    record_fire("s", _t(0, 0), tmp_path)
    record_fire("s", _t(0, 30), tmp_path)
    # 90 minutes after the first fire, only fires within the trailing hour count.
    assert fires_last_hour("s", _t(1, 30), tmp_path) == 1
    # Recording prunes first, then appends → the 0:30 survivor + the new one.
    assert record_fire("s", _t(1, 30), tmp_path) == 2


def test_missing_log_is_zero(tmp_path):
    assert fires_last_hour("nope", _t(), tmp_path) == 0
