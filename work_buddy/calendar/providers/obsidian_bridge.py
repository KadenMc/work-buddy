"""Obsidian-bridge calendar provider — the transitional bootstrap adapter.

Wraps the existing eval_js path in :mod:`work_buddy.calendar.env` (the Obsidian
google-calendar plugin) behind the provider-neutral :class:`CalendarProvider`
protocol. ``env.py`` and its ``_js/`` snippets are **not** modified — this
adapter is a mapping layer above them, so the working path stays intact while
the seam goes in on top (DECISIONS D3).

Plugin gotchas honored here:
* ``getEvent`` is **broken** — :meth:`get_event` filters a windowed
  :meth:`list_events` by id instead of calling it.
* ``get_events`` payload has **no iCalUID** — bridge events fall back to the
  ``loc:`` stable key; a native adapter supplies real UIDs later.
* ``getCalendars`` exposes no ``timeZone`` and the bridge can't introspect the
  plugin's ``calendarBlackList`` without new JS (frozen for PR #1), so
  :meth:`blacklisted_calendar_ids` approximates it from the ``selected`` flag.

Write methods are thin wraps of ``env.py``'s existing consent-gated writers.
They satisfy the protocol's ``isinstance`` contract but are **not** exposed
through the gateway in PR #1; ``env.py``'s ``@requires_consent`` stays dormant.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from work_buddy.calendar import env
from work_buddy.calendar.errors import (
    CalendarBridgeUnreachable,
    CalendarEventNotFound,
    CalendarProviderError,
)
from work_buddy.calendar.identity import stable_key_for
from work_buddy.calendar.models import CalendarEvent, CalendarRef, EventTime

_PROVIDER = "obsidian_bridge"
_SHARED_ROLES = {"reader", "writer", "freeBusyReader"}


def _map_runtime_error(exc: RuntimeError) -> Exception:
    """Translate ``env``/bridge ``RuntimeError`` into a typed CalendarError.

    ``env._run_js`` raises ``RuntimeError`` for two distinct failure classes:
    bridge-unreachable (from ``bridge.require_available``) and plugin/eval
    errors (``"Google Calendar error: ..."`` / ``"Eval error: ..."``). Map
    them so callers can ``isinstance``-classify.
    """
    msg = str(exc)
    if "Google Calendar error" in msg or "Eval error" in msg:
        return CalendarProviderError(msg)
    return CalendarBridgeUnreachable(msg)


def _event_time_from_google(obj: dict | None) -> EventTime:
    """Map a Google start/end object to :class:`EventTime`.

    ``{"date": "YYYY-MM-DD"}`` → all-day (tz-free).
    ``{"dateTime": "...±HH:MM", "timeZone": "..."}`` → timed; the offset in the
    ISO string is preserved verbatim (``fromisoformat``, no ``.astimezone()``).
    """
    if not obj:
        return EventTime()
    date = obj.get("date")
    if date and not obj.get("dateTime"):
        return EventTime(date=date)
    raw = obj.get("dateTime")
    if not raw:
        return EventTime()
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        # Unparseable datetime — degrade to a tz-free date so the event still
        # renders rather than crashing the whole window.
        return EventTime(date=str(raw)[:10])
    return EventTime(dt=dt, tz_name=obj.get("timeZone"))


def _event_from_bridge(e: dict) -> CalendarEvent:
    """Map one ``env.get_events`` event dict to :class:`CalendarEvent`."""
    calendar_id = e.get("calendarId") or ""
    provider_event_id = e.get("id") or ""
    ical_uid = e.get("iCalUID") or ""  # bridge payload lacks it today; future-proof
    return CalendarEvent(
        stable_key=stable_key_for(
            ical_uid=ical_uid,
            provider=_PROVIDER,
            calendar_id=calendar_id,
            provider_event_id=provider_event_id,
        ),
        provider=_PROVIDER,
        calendar_id=calendar_id,
        provider_event_id=provider_event_id,
        summary=e.get("summary") or "(no title)",
        start=_event_time_from_google(e.get("start")),
        end=_event_time_from_google(e.get("end")),
        status=e.get("status") or "confirmed",
        location=e.get("location") or "",
        description=e.get("description") or "",
        ical_uid=ical_uid,
        html_link=e.get("htmlLink") or "",
        calendar_name=e.get("calendarName") or "",
        transparency=e.get("transparency") or "",
    )


class ObsidianBridgeCalendarProvider:
    """``CalendarProvider`` backed by the Obsidian google-calendar plugin."""

    name = _PROVIDER

    # --- Discovery ---------------------------------------------------------

    def health(self) -> dict:
        try:
            return env.check_ready()
        except RuntimeError as exc:
            raise _map_runtime_error(exc) from exc

    def list_calendars(self) -> list[CalendarRef]:
        try:
            raw = env.get_calendars()
        except RuntimeError as exc:
            raise _map_runtime_error(exc) from exc
        refs: list[CalendarRef] = []
        for c in raw or []:
            is_primary = bool(c.get("primary"))
            access_role = c.get("accessRole") or ""
            refs.append(CalendarRef(
                id=c.get("id") or "",
                name=c.get("summaryOverride") or c.get("summary") or "(unnamed)",
                provider=_PROVIDER,
                is_primary=is_primary,
                access_role=access_role,
                is_shared=(not is_primary) and access_role in _SHARED_ROLES,
                color=c.get("backgroundColor"),
            ))
        return refs

    def blacklisted_calendar_ids(self) -> list[str]:
        """Approximate the plugin's ``calendarBlackList`` from the ``selected``
        flag — a deselected calendar is hidden from event fetches. This is an
        approximation (the authoritative list needs plugin-settings JS, frozen
        for PR #1); flagged in AFK-DECISIONS."""
        try:
            raw = env.get_calendars()
        except RuntimeError as exc:
            raise _map_runtime_error(exc) from exc
        return [c.get("id") or "" for c in (raw or []) if c.get("selected") is False]

    # --- Read --------------------------------------------------------------

    def list_events(
        self,
        *,
        start: str,
        end: str,
        calendar_ids: list[str] | None = None,
    ) -> list[CalendarEvent]:
        try:
            result = env.get_events(start, end)
        except RuntimeError as exc:
            raise _map_runtime_error(exc) from exc
        events = [_event_from_bridge(e) for e in (result or {}).get("events", [])]
        if calendar_ids is not None:
            wanted = set(calendar_ids)
            events = [ev for ev in events if ev.calendar_id in wanted]
        return events

    def get_event(
        self,
        *,
        calendar_id: str,
        event_id: str,
        window_days_back: int = 30,
        window_days_forward: int = 90,
    ) -> CalendarEvent:
        """Fetch one event by id.

        The plugin's ``getEvent`` is broken, so we fetch a window and filter.
        Without a date hint we scan a generous window around today (in the
        user's timezone); callers that know the date should prefer
        :meth:`list_events` with a tight range. Raises
        :class:`CalendarEventNotFound` when the id isn't in the window.
        """
        from work_buddy import config

        today = datetime.now(config.USER_TZ).date()
        start = (today - timedelta(days=window_days_back)).strftime("%Y-%m-%d")
        end = (today + timedelta(days=window_days_forward)).strftime("%Y-%m-%d")
        for ev in self.list_events(start=start, end=end, calendar_ids=[calendar_id]):
            if ev.provider_event_id == event_id:
                return ev
        raise CalendarEventNotFound(
            f"event {event_id!r} not found on calendar {calendar_id!r} within "
            f"{start}..{end}"
        )

    # --- Write (thin wraps; NOT gateway-exposed in PR #1) ------------------

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
        try:
            return env.create_event(
                summary=summary, start=start, end=end, calendar_id=calendar_id,
                description=description, location=location, all_day=all_day,
                timezone=timezone,
            )
        except RuntimeError as exc:
            raise _map_runtime_error(exc) from exc

    def update_event(
        self,
        *,
        calendar_id: str,
        event_id: str,
        changes: dict,
        notify: bool = False,
    ) -> dict:
        event = {"id": event_id, "parent": {"id": calendar_id}, **(changes or {})}
        try:
            return env.update_event(event, notify=notify)
        except RuntimeError as exc:
            raise _map_runtime_error(exc) from exc

    def delete_event(
        self,
        *,
        calendar_id: str,
        event_id: str,
        notify: bool = False,
    ) -> dict:
        event = {"id": event_id, "parent": {"id": calendar_id}}
        try:
            return env.delete_event(event, notify=notify)
        except RuntimeError as exc:
            raise _map_runtime_error(exc) from exc
