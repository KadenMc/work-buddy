---
name: Calendar List Events
kind: capability
description: List calendar events overlapping a date window (ISO YYYY-MM-DD start/end), optionally restricted to specific calendar ids. Returns canonical CalendarEvent dicts with offset-aware times preserved.
capability_name: calendar_list_events
category: calendar
parameters:
  start:
    type: str
    description: Window start, ISO date YYYY-MM-DD
    required: true
  end:
    type: str
    description: Window end, ISO date YYYY-MM-DD (inclusive)
    required: true
  calendar_ids:
    type: list
    description: Optional list of calendar ids to restrict to; omit for all subscribed calendars
    required: false
op: op.wb.calendar_list_events
schema_version: wb-capability/v1
tags:
- calendar
- events
- schedule
aliases:
- list calendar events
- calendar events
- what is on my calendar
- schedule for dates
- get events
parents:
- calendar
requires:
- google_calendar
---
