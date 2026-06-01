"""``bridge.require_available()`` raises typed ObsidianError, not RuntimeError.

The precondition guard used to raise a plain ``RuntimeError`` for a down
bridge, while the request-time failure path raised a typed
``ObsidianUnreachable`` subclass. The two classified differently in the
resilience breaker (plain RuntimeError → transient → trips; typed
ObsidianNotRunning → terminal → no trip). This locks in the unified contract:
``require_available()`` raises the same typed exception the request-time path
does, via the shared ``_classify_unreachable`` disambiguator.

Classification of a down bridge, by error kind:
- ``ObsidianNotRunning`` / plugin missing / disabled — "user must act out of
  band". Terminal for the breaker (no trip on a cheap clean-absence) AND
  permanent for the retry queue (no auto-enqueue) → the call fails fast with a
  clear message instead of churning 5 futile retries + an exhaustion alert.
- ``ObsidianStartupRace`` / ``ObsidianTimeout`` — a *temporarily*-unavailable
  bridge: transient → trips the breaker (sheds churn) AND enqueues (auto-
  recovers once the bridge is back). See ``work_buddy.errors.classify_error``.
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

    def test_typed_down_classifies_permanent_for_retry_queue(self):
        from work_buddy.errors import classify_error

        with patch.object(bridge, "is_obsidian_running", return_value=False):
            with pytest.raises(ObsidianNotRunning) as excinfo:
                bridge.require_available()
        # Retry-queue horizon: permanent — a deliberately-closed Obsidian is a
        # "user must act" condition, so it is NOT auto-enqueued (no 5x churn +
        # exhaustion notification). A transiently-unavailable bridge
        # (ObsidianStartupRace / ObsidianTimeout) stays transient and enqueues.
        assert classify_error(excinfo.value) == "permanent"


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
