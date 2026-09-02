---
name: Context Calendar
kind: capability
description: Provider-neutral Calendar schedule for a given date. With the current configuration it uses native Google OAuth and never calls Obsidian.
capability_name: context_calendar
category: context
op: op.wb.context_calendar
schema_version: wb-capability/v1
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
requires:
- google_calendar_native
---
