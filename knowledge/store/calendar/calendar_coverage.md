---
name: Calendar Coverage
kind: capability
description: Report which calendars work-buddy can actually see and how many events each yields in a window. Distinguishes subscribed vs blacklisted vs errored calendars. Defaults to a today..today+days window when start/end omitted.
capability_name: calendar_coverage
category: calendar
parameters:
  start:
    type: str
    description: Window start ISO date; defaults to today
    required: false
  end:
    type: str
    description: Window end ISO date; defaults to today+days
    required: false
  days:
    type: int
    description: Window length in days when start/end omitted (default 7)
    required: false
op: op.wb.calendar_coverage
schema_version: wb-capability/v1
tags:
- calendar
- coverage
- diagnostics
aliases:
- calendar coverage
- which calendars are visible
- calendar diagnostics
- calendar gaps
parents:
- calendar
requires:
- calendar
---
