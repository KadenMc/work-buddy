"""In-memory calendar provider for tests, dry runs, and local exploration.

Lets the collector → coverage → bundle pipeline be exercised without Obsidian.
Tests construct a provider with fixture calendars and events; production never
selects this provider unless ``calendar.provider: fake`` is set explicitly.
Mirrors :class:`work_buddy.email.providers.fake.FakeEmailProvider`.

Write methods mutate in-memory state so PR #2's write-capability tests can drive
this provider too; ``health()`` is always ready unless a test overrides it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from work_buddy.calendar.errors import CalendarEventNotFound
from work_buddy.calendar.identity import stable_key_for
from work_buddy.calendar.models import CalendarEvent, CalendarRef, EventTime


class FakeCalendarProvider:
    """Provider with a hand-loaded calendar/event store. No network, no I/O."""

    name = "fake"

    def __init__(self) -> None:
        self._calendars: list[CalendarRef] = []
        self._events: list[CalendarEvent] = []
        self._blacklist: list[str] = []
        self._healthy: bool = True

    # --- Seeders (test-only) ----------------------------------------------

    def add_calendar(
        self,
        id: str,
        name: str,
        *,
        is_primary: bool = False,
        access_role: str = "owner",
        is_shared: bool = False,
        color: str | None = None,
    ) -> CalendarRef:
        ref = CalendarRef(
            id=id, name=name, provider=self.name, is_primary=is_primary,
            access_role=access_role, is_shared=is_shared, color=color,
        )
        self._calendars.append(ref)
        return ref

    def blacklist(self, calendar_id: str) -> None:
        self._blacklist.append(calendar_id)

    def add_event(self, event: CalendarEvent) -> CalendarEvent:
        self._events.append(event)
        return event

    def add_events(self, events: Iterable[CalendarEvent]) -> None:
        for e in events:
            self.add_event(e)

    def set_healthy(self, healthy: bool) -> None:
        self._healthy = healthy

    # --- CalendarProvider --------------------------------------------------

    def health(self) -> dict:
        return {
            "ready": self._healthy,
            "calendar_count": len(self._calendars),
            "version": "fake",
        }

    def list_calendars(self) -> list[CalendarRef]:
        return [replace(c) for c in self._calendars]

    def blacklisted_calendar_ids(self) -> list[str]:
        return list(self._blacklist)

    def list_events(
        self,
        *,
        start: str,
        end: str,
        calendar_ids: list[str] | None = None,
    ) -> list[CalendarEvent]:
        wanted = set(calendar_ids) if calendar_ids is not None else None
        out: list[CalendarEvent] = []
        for e in self._events:
            if e.calendar_id in self._blacklist:
                continue
            if wanted is not None and e.calendar_id not in wanted:
                continue
            # Overlap test on YYYY-MM-DD day keys (lexicographic order is valid
            # for ISO dates): event_start_day <= end AND event_end_day >= start.
            s_key = e.start.date_key
            e_key = e.end.date_key if e.end.date_key != "unknown" else s_key
            if s_key <= end and e_key >= start:
                out.append(e)
        out.sort(key=lambda ev: ev.start.sort_value)
        return out

    def get_event(self, *, calendar_id: str, event_id: str) -> CalendarEvent:
        for e in self._events:
            if e.calendar_id == calendar_id and e.provider_event_id == event_id:
                return e
        raise CalendarEventNotFound(
            f"fake: no event {event_id!r} on calendar {calendar_id!r}"
        )

    # --- Write (state-mutating; exercised by PR #2 tests) ------------------

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
        cal_id = calendar_id or (self._calendars[0].id if self._calendars else "primary")
        event_id = f"fake-evt-{len(self._events) + 1}"
        # All-day → store a bare date; timed → parse offset-aware, falling back
        # to a day-key on unparseable input.
        if not all_day and "T" in start:
            from datetime import datetime
            try:
                start_t = EventTime(dt=datetime.fromisoformat(start), tz_name=timezone)
                end_t = EventTime(dt=datetime.fromisoformat(end), tz_name=timezone)
            except ValueError:
                start_t = EventTime(date=start[:10])
                end_t = EventTime(date=end[:10])
        else:
            start_t = EventTime(date=start)
            end_t = EventTime(date=end)
        ev = CalendarEvent(
            stable_key=stable_key_for(
                ical_uid="", provider=self.name,
                calendar_id=cal_id, provider_event_id=event_id,
            ),
            provider=self.name,
            calendar_id=cal_id,
            provider_event_id=event_id,
            summary=summary,
            start=start_t,
            end=end_t,
            location=location,
            description=description,
            wb_origin=True,
        )
        self._events.append(ev)
        return {"success": True, "id": event_id, "summary": summary, "calendarId": cal_id}

    def update_event(
        self,
        *,
        calendar_id: str,
        event_id: str,
        changes: dict,
        notify: bool = False,
    ) -> dict:
        ev = self.get_event(calendar_id=calendar_id, event_id=event_id)
        if "summary" in changes:
            ev.summary = changes["summary"]
        if "location" in changes:
            ev.location = changes["location"]
        if "description" in changes:
            ev.description = changes["description"]
        return {"success": True, "id": event_id, "notified": notify}

    def delete_event(
        self,
        *,
        calendar_id: str,
        event_id: str,
        notify: bool = False,
    ) -> dict:
        before = len(self._events)
        self._events = [
            e for e in self._events
            if not (e.calendar_id == calendar_id and e.provider_event_id == event_id)
        ]
        if len(self._events) == before:
            raise CalendarEventNotFound(
                f"fake: no event {event_id!r} on calendar {calendar_id!r}"
            )
        return {"success": True, "deleted_id": event_id, "notified": notify}
