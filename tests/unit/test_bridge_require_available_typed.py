"""``bridge.require_available()`` raises typed ObsidianError, not RuntimeError.

The precondition guard used to raise a plain ``RuntimeError`` for a down
bridge, while the request-time failure path raised a typed
``ObsidianUnreachable`` subclass. The two classified differently in the
resilience breaker (plain RuntimeError → transient → trips; typed
ObsidianNotRunning → terminal → no trip). This locks in the unified contract:
``require_available()`` raises the same typed exception the request-time path
does, via the shared ``_classify_unreachable`` disambiguator.

Design note: a down bridge is deliberately classified two ways, by horizon —
TERMINAL for the resilience breaker / inner retry (a cheap process check; no
point short-retrying or shedding), but TRANSIENT for the retry QUEUE (the op
will succeed once the user opens Obsidian). Both are honored here.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from work_buddy.obsidian import bridge
from work_buddy.obsidian.errors import (
    ObsidianError,
    ObsidianNotRunning,
    ObsidianUnreachable,
)


class TestRequireAvailableTyped:
    def test_process_down_raises_typed_not_running(self):
        with patch.object(bridge, "is_obsidian_running", return_value=False):
            with pytest.raises(ObsidianNotRunning) as excinfo:
                bridge.require_available()
        exc = excinfo.value
        assert isinstance(exc, ObsidianUnreachable)
        assert isinstance(exc, ObsidianError)
        # NOT a plain RuntimeError anymore (that was the inconsistency).
        assert not isinstance(exc, RuntimeError)

    def test_typed_down_classifies_terminal_for_breaker(self):
        from work_buddy.obsidian.resilient_bridge import classify_obsidian_error
        from work_buddy.resilience import OutcomeKind

        with patch.object(bridge, "is_obsidian_running", return_value=False):
            with pytest.raises(ObsidianNotRunning) as excinfo:
                bridge.require_available()
        # Breaker horizon: terminal — a cheap clean-absence, nothing to shed.
        assert classify_obsidian_error(excinfo.value) is OutcomeKind.TERMINAL_FAILURE

    def test_typed_down_classifies_transient_for_retry_queue(self):
        from work_buddy.errors import classify_error

        with patch.object(bridge, "is_obsidian_running", return_value=False):
            with pytest.raises(ObsidianNotRunning) as excinfo:
                bridge.require_available()
        # Retry-queue horizon: transient — the op completes when the user
        # opens Obsidian (the queued retry replays successfully).
        assert classify_error(excinfo.value) == "transient"


class TestCalendarProviderMapsTypedUnreachable:
    def test_bridge_down_maps_to_calendar_bridge_unreachable(self):
        from work_buddy.calendar.providers.obsidian_bridge import (
            ObsidianBridgeCalendarProvider,
        )
        from work_buddy.calendar.errors import CalendarBridgeUnreachable

        provider = ObsidianBridgeCalendarProvider()
        with patch.object(bridge, "is_obsidian_running", return_value=False):
            # The typed ObsidianUnreachable from require_available must be
            # caught + mapped, not propagated raw past the provider seam.
            with pytest.raises(CalendarBridgeUnreachable):
                provider.list_calendars()
