---
name: Google Calendar Integration
kind: integration
description: Google Calendar access via Obsidian Google Calendar plugin (stale/unmaintained) + eval_js bridge
tags:
- obsidian
- calendar
- google
- eval_js
- consent
aliases:
- google calendar
- calendar plugin
- calendar integration
parents:
- obsidian
- obsidian
dev_notes: '`is_shared` heuristic in the adapter = `(not primary) AND access_role in {reader, writer, freeBusyReader}` — approximate; a non-primary calendar you *own* is NOT flagged shared. Validated against the live calendars (SickKids/UHN/U of T/True Peace flagged shared; primary + personal owner cals not). The `blacklisted_calendar_ids` adapter method approximates the plugin''s `calendarBlackList` from the `selected === false` flag (reading the authoritative setting would need a new `_js` snippet). Coverage is built from a single bulk `get_events` (the plugin returns all calendars in one round-trip), not per-calendar fetches.'
---

Google Calendar access via the Obsidian Google Calendar plugin (v1.10.16) over the eval_js bridge. **This is the `obsidian_bridge` *adapter* beneath the provider-neutral `calendar` subsystem** — see `calendar` for the seam (canonical models + `CalendarProvider` protocol + factory). Consumers depend on `work_buddy.calendar.provider` + `work_buddy.calendar.models`, **not** on `env.py` directly.

## Warning

The Google Calendar Obsidian plugin is stale/unmaintained and could stop working at any time. It is a **transitional** adapter; a native own-OAuth adapter is planned to replace it and retire the bridge at read+write parity. Plan for graceful degradation.

## Where it sits

`work_buddy/calendar/providers/obsidian_bridge.py` (`ObsidianBridgeCalendarProvider`) wraps `work_buddy/calendar/env.py` (Python eval_js wrappers) + `_js/` (snippets executed inside Obsidian). Prerequisites: Obsidian running with the work-buddy bridge, Google Calendar plugin installed + authenticated. The adapter maps the plugin's calendar/event dicts to the canonical `CalendarEvent`/`CalendarRef` models.

## Runtime API Surface

Plugin exposes `app.plugins.plugins["google-calendar"].api` with: getCalendars, getEvents (requires Moment.js dates), createEvent, updateEvent, deleteEvent, createEventNote. **getEvent is broken** (returns error for all events) — the adapter's `get_event` filters a windowed `getEvents` by id instead. The payload carries **no iCalUID**, so bridge events use the `loc:<provider>:<calendar_id>:<event_id>` stable key (native supplies real `ical:` UIDs later).

## Event Object Structure

Google Calendar API v3 schema plus a `parent` field: id, summary, status, htmlLink, start/end (dateTime/date/timeZone), location, description, colorId, transparency, eventType, parent (calendar metadata).

## Gateway surface

Reads **and writes** are exposed through the `calendar` subsystem capabilities (`calendar_health`, `calendar_list_events`, `calendar_get_event`, `calendar_coverage`, and `create`/`update`/`delete_calendar_event`), gated by the provider-aware `calendar` tool probe (not `google_calendar`). The write capabilities carry heavy per-change consent in `capabilities.py` (one layer up, so every provider inherits identical gating). `env.create_event/update_event/delete_event` no longer carry their own `@requires_consent` — the semantic gate moved up to the capability layer, and the writers now carry `@reduces_risk_for("obsidian.eval_js", "low")` so their internal eval_js passes without a second prompt.

## Degradation

All functions degrade gracefully: bridge unavailable → typed `CalendarBridgeUnreachable`. Plugin not installed → `check_ready` returns `{ready: false}`. Not authenticated → getCalendars fails, caught and reported. Stale plugin → eval_js timeout, caught by the bridge layer. The collector falls back to a clear 'not available' report rather than crashing the morning bundle.
