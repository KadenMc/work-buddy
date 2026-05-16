"""Tests for timezone validation — the runtime must never crash on a
bad configured timezone.

An invalid ``timezone`` in config.yaml previously made ``ZoneInfo()``
raise on every scheduler tick, silently halting all scheduled jobs.
``safe_timezone`` validates once and degrades to UTC instead.
"""

from __future__ import annotations

import logging

from work_buddy.config import safe_timezone
from work_buddy.sidecar.scheduler.engine import Scheduler


class TestSafeTimezone:
    def test_valid_timezone_passes_through(self):
        assert safe_timezone("America/New_York") == "America/New_York"
        assert safe_timezone("Europe/London") == "Europe/London"
        assert safe_timezone("UTC") == "UTC"

    def test_invalid_timezone_falls_back_to_utc(self):
        assert safe_timezone("WB_TEST_INVALID_TZ") == "UTC"
        assert safe_timezone("Not/AZone") == "UTC"

    def test_invalid_timezone_logs_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="work_buddy.config"):
            safe_timezone("Bogus/Zone")
        assert any("Bogus/Zone" in r.message for r in caplog.records)
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_none_and_empty_fall_back_silently(self, caplog):
        with caplog.at_level(logging.WARNING, logger="work_buddy.config"):
            assert safe_timezone(None) == "UTC"
            assert safe_timezone("") == "UTC"
        # Absence is not an error — a missing value gets no warning.
        assert not caplog.records

    def test_custom_fallback_is_honored(self):
        assert safe_timezone("Bogus/Zone", fallback="America/Toronto") == "America/Toronto"
        assert safe_timezone(None, fallback="America/Toronto") == "America/Toronto"


class TestSchedulerTimezone:
    """The engine validates the configured timezone once at construction
    (and on hot-reload) so cron matching never throws per-tick."""

    def test_invalid_config_timezone_degrades_to_utc(self):
        engine = Scheduler({"timezone": "WB_TEST_INVALID_TZ"})
        assert engine._timezone == "UTC"

    def test_valid_config_timezone_is_kept(self):
        engine = Scheduler({"timezone": "America/New_York"})
        assert engine._timezone == "America/New_York"

    def test_missing_config_timezone_degrades_to_utc(self):
        engine = Scheduler({})
        assert engine._timezone == "UTC"
