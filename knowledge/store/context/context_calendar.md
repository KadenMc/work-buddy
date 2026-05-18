---
name: Context Calendar
kind: capability
description: Google Calendar schedule for a given date. Also checks plugin readiness.
capability_name: context_calendar
category: context
parameters:
  date:
    type: str
    description: 'Date (YYYY-MM-DD). Default: today.'
    required: false
  check_ready:
    type: bool
    description: Return only readiness check, no schedule fetch (default false)
    required: false
tags:
- context
- calendar
aliases:
- today's schedule
- calendar events
- meetings today
- what's on the calendar
- calendar ready
parents:
- context
- context
requires:
- obsidian
- google_calendar
---
