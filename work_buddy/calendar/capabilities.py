"""Read-only capability callables for the calendar integration.

Registered as ops in :mod:`work_buddy.mcp_server.ops.calendar_ops` and declared
as ``kind: capability`` knowledge units (gated by the ``google_calendar`` tool
probe). All callables instantiate the configured provider on demand and return
JSON-serialisable dicts, mirroring :mod:`work_buddy.email.capabilities`.

Read-only surface:
  - ``calendar_health``       Provider readiness payload.
  - ``calendar_list_events``  Events in a date window (optionally per calendar).
  - ``calendar_get_event``    One event by id.
  - ``calendar_coverage``     Which calendars are visible / blacklisted / errored.

Write capabilities (heavy-consent mutation) belong one layer up from the adapter
so every provider inherits identical consent gating; they are not part of this
read-only surface.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from work_buddy.calendar.errors import CalendarError, CalendarEventNotFound
from work_buddy.calendar.models import CoverageReport
from work_buddy.calendar.provider import get_calendar_provider

log = logging.getLogger(__name__)


def _provider_or_error() -> tuple[Any, dict | None]:
    try:
        return get_calendar_provider(), None
    except CalendarError as exc:
        return None, {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


def _err(exc: CalendarError) -> dict:
    return {"ok": False, "error": str(exc), "error_kind": exc.error_kind}


# ---------------------------------------------------------------------------
# Read-only diagnostics
# ---------------------------------------------------------------------------


def calendar_health() -> dict:
    """Liveness probe — return the configured provider's readiness payload.

    Distinguishes "bridge down" from "no calendars" the way email_health does
    for the mail bridge."""
    provider, err = _provider_or_error()
    if err:
        return err
    try:
        return {"ok": True, "provider": provider.name, **provider.health()}
    except CalendarError as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_calendar_events(
    *,
    start: str,
    end: str,
    calendar_ids: list[str] | None = None,
) -> dict:
    """Return events overlapping ``[start, end]`` (ISO ``YYYY-MM-DD`` dates).

    Optionally restrict to ``calendar_ids``. Each event is the canonical
    ``CalendarEvent.to_dict()`` shape (offset-aware times preserved)."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not start or not end:
        return {"ok": False, "error": "start and end (YYYY-MM-DD) are required",
                "error_kind": "bad_request"}
    try:
        events = provider.list_events(start=start, end=end, calendar_ids=calendar_ids)
        return {
            "ok": True,
            "provider": provider.name,
            "window": {"start": start, "end": end},
            "count": len(events),
            "events": [e.to_dict() for e in events],
        }
    except CalendarError as exc:
        return _err(exc)


def get_calendar_event(*, calendar_id: str, event_id: str) -> dict:
    """Fetch one event by its provider-local id (within the adapter's lookup
    window, since the plugin's getEvent is broken)."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not calendar_id or not event_id:
        return {"ok": False, "error": "calendar_id and event_id are required",
                "error_kind": "bad_request"}
    try:
        ev = provider.get_event(calendar_id=calendar_id, event_id=event_id)
        return {"ok": True, "provider": provider.name, "event": ev.to_dict()}
    except CalendarEventNotFound as exc:
        return _err(exc)
    except CalendarError as exc:
        return _err(exc)


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def build_coverage_report(provider, start: str, end: str) -> CoverageReport:
    """Compute coverage from a **single bulk fetch** and count per calendar.

    The bridge's ``get_events`` returns every subscribed calendar's events in
    one round-trip, so fetching per calendar would be N redundant calls. We
    bulk-fetch once and tally by ``calendar_id``; calendars with no events
    still appear (count 0). ``errored`` only populates on a *total* window
    failure — per-calendar error isolation becomes meaningful with the native
    adapter (separate API calls per calendar) and is wired then. ``subscribed``
    / ``blacklisted`` are best-effort and degrade independently.
    """
    try:
        subscribed = provider.list_calendars()
    except CalendarError:
        subscribed = []
    try:
        blacklisted = provider.blacklisted_calendar_ids()
    except CalendarError:
        blacklisted = []

    bl = set(blacklisted)
    per_calendar: dict[str, int] = {ref.id: 0 for ref in subscribed if ref.id not in bl}
    errored: dict[str, str] = {}
    total = 0
    try:
        events = provider.list_events(start=start, end=end)
        for e in events:
            if e.calendar_id in bl:
                continue
            per_calendar[e.calendar_id] = per_calendar.get(e.calendar_id, 0) + 1
            total += 1
    except CalendarError as exc:
        # Whole-window fetch failed — attribute to each visible calendar so the
        # report shows the gap rather than silently reporting zero events.
        for ref in subscribed:
            if ref.id not in bl:
                errored[ref.id] = str(exc)

    return CoverageReport(
        window={"start": start, "end": end},
        subscribed=subscribed,
        blacklisted=blacklisted,
        per_calendar_counts=per_calendar,
        errored=errored,
        total_events=total,
    )


def calendar_coverage(
    *,
    start: str | None = None,
    end: str | None = None,
    days: int = 7,
) -> dict:
    """Report which calendars WB can see and how many events each yields.

    Defaults to a ``today .. today+days`` window in the user's timezone when
    ``start`` / ``end`` are omitted."""
    provider, err = _provider_or_error()
    if err:
        return err
    if not start or not end:
        from work_buddy import config
        today = datetime.now(config.USER_TZ).date()
        start = start or today.strftime("%Y-%m-%d")
        end = end or (today + timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        rep = build_coverage_report(provider, start, end)
        return {"ok": True, "provider": provider.name, **rep.to_dict()}
    except CalendarError as exc:
        return _err(exc)
