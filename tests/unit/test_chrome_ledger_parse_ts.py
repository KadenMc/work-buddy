"""Pin chrome_ledger._parse_ts local-time behavior after the timefmt migration.

``_parse_ts`` renders ledger timestamps in the user's local zone via the shared
helper. The conversion only fires for tz-aware inputs — a naive string is left
naive (unconverted), the behavior callers have always relied on. America/Toronto
(UTC-4 in June) is pinned so a regression to UTC fails loudly.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import work_buddy.config as config
from work_buddy.collectors import chrome_ledger


@pytest.fixture
def pin_tz(monkeypatch):
    monkeypatch.setattr(config, "_USER_TZ_CACHE", ZoneInfo("America/Toronto"))


def test_parse_ts_converts_aware_to_local(pin_tz):
    assert chrome_ledger._parse_ts("2026-06-05T17:30:00Z") == datetime(2026, 6, 5, 13, 30)


def test_parse_ts_converts_explicit_offset(pin_tz):
    # 17:30-00:00 → 13:30 local; result is naive local.
    out = chrome_ledger._parse_ts("2026-06-05T17:30:00+00:00")
    assert out == datetime(2026, 6, 5, 13, 30)
    assert out.tzinfo is None


def test_parse_ts_leaves_naive_unconverted(pin_tz):
    # No offset in the string → naive → guard skips conversion (unchanged behavior).
    assert chrome_ledger._parse_ts("2026-06-05T17:30:00") == datetime(2026, 6, 5, 17, 30)
