"""Google Calendar plugin (v1.10.16) runtime access via eval_js.

All functions require Obsidian to be running with the work-buddy plugin active.
The Google Calendar plugin must be installed and authenticated with Google.

Discovered runtime surface (plugin.api):
- getCalendars() — list all calendars
- getEvents({startDate, endDate}) — fetch events in range (Moment.js dates)
- getEvent(calendarId, eventId) — fetch single event
- createEvent(event) — create an event
- updateEvent(event, notify?) — update an event
- deleteEvent(event, notify?) — delete an event
- createEventNote(event, calendarId, confirm?) — create Obsidian note for event

Event objects follow the Google Calendar API v3 schema, plus a `parent`
field containing the calendar metadata.
"""

import json
from pathlib import Path
from typing import Any

from work_buddy.obsidian import bridge
from work_buddy.consent import requires_consent
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_JS_DIR = Path(__file__).parent / "_js"


def _load_js(name: str) -> str:
    """Load a JS snippet from the _js directory."""
    return (_JS_DIR / name).read_text(encoding="utf-8")


def _run_js(
    snippet_name: str,
    replacements: dict[str, str] | None = None,
    timeout: int = 15,
) -> Any:
    """Load a JS snippet, apply replacements, execute via eval_js.

    Raises RuntimeError if the bridge is unavailable or the JS returns an error.
    """
    bridge.require_available()
    js = _load_js(snippet_name)
    for placeholder, value in (replacements or {}).items():
        js = js.replace(placeholder, value)
    result = bridge.eval_js(js, timeout=timeout)
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"Google Calendar error: {result['error']}")
    return result


# ── Readiness ───────────────────────────────────────────────────


def check_ready() -> dict[str, Any]:
    """Check if the Google Calendar plugin is loaded and authenticated.

    Returns a dict with:
    - ready: bool — True when the plugin can fetch calendars
    - calendar_count: int — number of accessible calendars
    - version: str — plugin version
    - reason: str — explanation if not ready
    """
    bridge.require_available()
    result = bridge.eval_js(_load_js("check_ready.js"), timeout=15)
    if result is None:
        return {"ready": False, "reason": "eval_js returned None"}
    if result.get("ready") and result.get("version"):
        from work_buddy.obsidian.plugin_versions import confirm_working
        confirm_working("google-calendar", result["version"])
    return result


# ── Queries ─────────────────────────────────────────────────────


def get_calendars() -> list[dict[str, Any]]:
    """List all Google Calendar calendars the user has access to.

    Returns list of dicts with: id, summary, summaryOverride, primary,
    backgroundColor, accessRole, selected.
    """
    result = _run_js("get_calendars.js")
    return result.get("calendars", [])


def get_events(start_date: str, end_date: str) -> dict[str, Any]:
    """Fetch events for a date range.

    Args:
        start_date: ISO date string, e.g. "2026-04-04".
        end_date: ISO date string, e.g. "2026-04-04".

    Returns:
        Dict with 'count', 'range', and 'events' (list of event dicts).
        Each event has: id, summary, status, start, end, location,
        description, htmlLink, calendarId, calendarName, eventType,
        isAllDay, colorId, transparency.
    """
    return _run_js(
        "get_events.js",
        {"__START_DATE__": start_date, "__END_DATE__": end_date},
    )


def get_today_schedule() -> dict[str, Any]:
    """Get today's events sorted by time, with schedule metadata.

    Returns:
        Dict with: date, currentTime, count, allDayCount, timedCount,
        upcomingCount, currentCount, and events (list sorted by time,
        each with a timeStatus of 'all-day', 'past', 'current', or 'upcoming').
    """
    return _run_js("get_today_schedule.js")


# ── Mutations ──────────────────────────────────────────────────


@requires_consent(
    operation="calendar.create_event",
    reason="Create a new event on your Google Calendar.",
    risk="high",
    default_ttl=10,
)
def create_event(
    summary: str,
    start: str,
    end: str,
    calendar_id: str | None = None,
    description: str = "",
    location: str = "",
    all_day: bool = False,
    timezone: str = "America/Toronto",
) -> dict[str, Any]:
    """Create a new Google Calendar event.

    Args:
        summary: Event title.
        start: Start time. For timed events: ISO datetime (e.g. "2026-04-05T14:00:00").
               For all-day events: ISO date (e.g. "2026-04-05").
        end: End time. Same format as start.
        calendar_id: Calendar to create the event in. If None, uses primary calendar.
        description: Event description (optional).
        location: Event location (optional).
        all_day: If True, create an all-day event (start/end are dates, not datetimes).
        timezone: IANA timezone for timed events (default: America/Toronto).

    Returns:
        Dict with: success, id, summary, start, end, htmlLink, calendarId.
    """
    if calendar_id is None:
        calendars = get_calendars()
        primary = next((c for c in calendars if c.get("primary")), None)
        calendar_id = primary["id"] if primary else calendars[0]["id"]

    return _run_js(
        "create_event.js",
        {
            "__SUMMARY__": _escape_js(summary),
            "__START__": start,
            "__END__": end,
            "__CALENDAR_ID__": calendar_id,
            "__DESCRIPTION__": _escape_js(description),
            "__LOCATION__": _escape_js(location),
            "__ALL_DAY__": "true" if all_day else "false",
            "__TIMEZONE__": timezone,
        },
        timeout=20,
    )


@requires_consent(
    operation="calendar.update_event",
    reason="Modify an existing event on your Google Calendar.",
    risk="high",
    default_ttl=10,
)
def update_event(
    event: dict[str, Any],
    notify: bool = False,
) -> dict[str, Any]:
    """Update an existing Google Calendar event.

    Pass the full event object (as returned by get_events) with your changes applied.
    The event must have an 'id' field.

    Args:
        event: Full event object with modifications applied.
               Must include 'id' and 'parent.id' (calendar ID).
        notify: If True, send update notifications to attendees.

    Returns:
        Dict with: success, id, summary, start, end, htmlLink, notified.
    """
    event_json = json.dumps(event).replace("'", "\\'")
    return _run_js(
        "update_event.js",
        {
            "__EVENT_JSON__": event_json,
            "__NOTIFY__": "true" if notify else "false",
        },
        timeout=20,
    )


@requires_consent(
    operation="calendar.delete_event",
    reason="Permanently delete an event from your Google Calendar.",
    risk="high",
    default_ttl=5,
)
def delete_event(
    event: dict[str, Any],
    notify: bool = False,
) -> dict[str, Any]:
    """Delete a Google Calendar event.

    Args:
        event: Event object with at least 'id' and 'parent.id' fields.
        notify: If True, send cancellation notifications to attendees.

    Returns:
        Dict with: success, deleted_id, summary, notified.
    """
    event_json = json.dumps(event).replace("'", "\\'")
    return _run_js(
        "delete_event.js",
        {
            "__EVENT_JSON__": event_json,
            "__NOTIFY__": "true" if notify else "false",
        },
        timeout=20,
    )


@requires_consent(
    operation="calendar.create_event_note",
    reason="Create an Obsidian note linked to a Google Calendar event.",
    risk="high",
    default_ttl=10,
)
def create_event_note(
    event: dict[str, Any],
    calendar_id: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create an Obsidian note for a Google Calendar event.

    Uses the plugin's note creation template (configured in plugin settings).

    Args:
        event: Event object with at least 'id' and 'summary'.
        calendar_id: Calendar ID the event belongs to. If None, uses event.parent.id.
        confirm: If True, open a confirmation dialog in Obsidian before creating.

    Returns:
        Dict with: success, event_id, summary, calendar_id, confirmed.
    """
    cal_id = calendar_id or event.get("parent", {}).get("id", "")
    event_json = json.dumps(event).replace("'", "\\'")
    return _run_js(
        "create_event_note.js",
        {
            "__EVENT_JSON__": event_json,
            "__CALENDAR_ID__": cal_id,
            "__CONFIRM__": "true" if confirm else "false",
        },
        timeout=20,
    )


# ── Helpers ────────────────────────────────────────────────────


def _escape_js(text: str) -> str:
    """Escape text for safe insertion into a JS string literal."""
    return (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
