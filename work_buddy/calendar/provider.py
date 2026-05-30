"""Provider protocol + factory.

Consumers (the calendar collector, the coverage capability) depend on this
protocol; the concrete backend is selected via ``calendar.provider`` in config.
Test code registers
:class:`work_buddy.calendar.providers.fake.FakeCalendarProvider` and exercises
the full pipeline without Obsidian.

The protocol declares the **full** surface — reads *and* writes — so the
contract is stable even though only reads are wired through the gateway today.
There is deliberately **no** ``ensure_calendar`` / provisioning method: WB
writes to the user's real calendars, it does not create a sandbox calendar.
Mirrors :func:`work_buddy.email.provider.get_email_provider`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from work_buddy.calendar.models import CalendarEvent, CalendarRef


@runtime_checkable
class CalendarProvider(Protocol):
    """Stable interface every calendar backend must implement.

    Methods raise typed :class:`work_buddy.calendar.errors.CalendarError`
    subclasses on failure so capability wrappers can ``isinstance``-classify
    and pick appropriate retry / display behavior.
    """

    name: str
    """Short identifier for diagnostics, e.g. ``"obsidian_bridge"``."""

    # --- Discovery ---------------------------------------------------------

    def health(self) -> dict:
        """Quick liveness check. Returns the backend's readiness payload."""

    def list_calendars(self) -> list[CalendarRef]:
        """Return one :class:`CalendarRef` per calendar the backend exposes."""

    def blacklisted_calendar_ids(self) -> list[str]:
        """Calendar ids the backend hides from event fetches (e.g. the
        plugin's ``calendarBlackList``). Empty for providers without the
        concept. Used by the coverage report to explain gaps."""

    # --- Read --------------------------------------------------------------

    def list_events(
        self,
        *,
        start: str,
        end: str,
        calendar_ids: list[str] | None = None,
    ) -> list[CalendarEvent]:
        """Return events overlapping the ``[start, end]`` window (ISO date
        strings, ``YYYY-MM-DD``). When ``calendar_ids`` is given, restrict to
        those calendars; otherwise return all subscribed calendars' events."""

    def get_event(self, *, calendar_id: str, event_id: str) -> CalendarEvent:
        """Fetch a single event by its provider-local id. Raises
        :class:`CalendarEventNotFound` when absent."""

    # --- Write (declared for contract stability; not gateway-exposed) ------

    def create_event(
        self,
        *,
        summary: str,
        start: str,
        end: str,
        calendar_id: str | None = None,
        description: str = "",
        location: str = "",
        all_day: bool = False,
        timezone: str | None = None,
    ) -> dict:
        """Create an event on a real calendar. Heavy-consent-gated one layer
        up (capabilities), not in the adapter. Raises
        :class:`CalendarWriteUnsupported` on read-only providers."""

    def update_event(
        self,
        *,
        calendar_id: str,
        event_id: str,
        changes: dict,
        notify: bool = False,
    ) -> dict:
        """Apply ``changes`` to an existing event. Raises
        :class:`CalendarWriteUnsupported` on read-only providers."""

    def delete_event(
        self,
        *,
        calendar_id: str,
        event_id: str,
        notify: bool = False,
    ) -> dict:
        """Delete an event. Raises :class:`CalendarWriteUnsupported` on
        read-only providers."""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_calendar_provider() -> CalendarProvider:
    """Return the configured calendar provider.

    Selection is driven by ``calendar.provider`` in config (default
    ``"obsidian_bridge"``). ``calendar.enabled: false`` short-circuits with
    :class:`CalendarProviderDisabled` so the morning bundle degrades cleanly.
    The gateway's tool-probe layer (``requires: [google_calendar]`` on each
    capability) is the correct place to short-circuit before reaching this
    factory; callers should ``isinstance``-check raised errors rather than
    swallowing them.

    Tests can override by importing :class:`FakeCalendarProvider` directly and
    bypassing this factory.
    """
    from work_buddy.config import load_config
    from work_buddy.calendar.errors import CalendarProviderDisabled

    cfg = (load_config() or {}).get("calendar", {}) or {}
    if cfg.get("enabled", True) is False:
        raise CalendarProviderDisabled("calendar.enabled is False in config")

    name = (cfg.get("provider") or "obsidian_bridge").lower()
    if name in ("obsidian_bridge", "obsidian", "bridge"):
        from work_buddy.calendar.providers.obsidian_bridge import (
            ObsidianBridgeCalendarProvider,
        )
        return ObsidianBridgeCalendarProvider()
    if name in ("google_native", "google", "native"):
        from work_buddy.calendar.providers.google_native import (
            GoogleNativeCalendarProvider,
        )
        return GoogleNativeCalendarProvider(cfg.get("google_native", {}))
    if name == "fake":
        from work_buddy.calendar.providers.fake import FakeCalendarProvider
        return FakeCalendarProvider()
    raise CalendarProviderDisabled(f"Unknown calendar.provider: {name!r}")
