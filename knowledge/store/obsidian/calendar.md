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
---

Google Calendar integration via Obsidian Google Calendar plugin (v1.10.16) + eval_js bridge.

## Warning

The Google Calendar Obsidian plugin is in stale/unmaintained mode and could stop working at any time. Plan for graceful degradation.

## Architecture

work_buddy/calendar/ with env.py (Python wrappers) and _js/ (JS snippets executed inside Obsidian). Prerequisites: Obsidian running with work-buddy bridge (port 27125), Google Calendar plugin installed + authenticated.

## Runtime API Surface

Plugin exposes app.plugins.plugins["google-calendar"].api with: getCalendars, getEvents (requires Moment.js dates), createEvent, updateEvent, deleteEvent, createEventNote. getEvent is broken (returns error for all events) -- use getEvents with date range instead.

## Event Object Structure

Google Calendar API v3 schema plus parent field: id, summary, status, htmlLink, start/end (dateTime/date/timeZone), location, description, colorId, transparency, eventType, parent (calendar metadata).

## Write Operations (consent-gated)

All writes are risk="high": calendar.create_event (10min TTL), calendar.update_event (10min), calendar.delete_event (5min), calendar.create_event_note (10min).

## Degradation

All functions degrade gracefully: bridge unavailable -> clear error. Plugin not installed -> check_ready returns {ready: false}. Not authenticated -> getCalendars fails, caught and reported. Stale plugin -> eval_js timeout, caught by bridge layer.
