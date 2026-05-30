---
name: Calendar Create Event
kind: capability
description: Create a new event on one of the user's real calendars. Heavy per-change consent — the approval prompt shows the target calendar, title, and start–end in the user's timezone, with a shared-calendar warning when others can see the calendar.
capability_name: create_calendar_event
category: calendar
op: op.wb.calendar_create_event
schema_version: wb-capability/v1
mutates_state: true
consent_required: true
parameters:
  summary:
    type: str
    description: Event title
    required: true
  start:
    type: str
    description: 'Start: ISO datetime for timed events, or YYYY-MM-DD for all-day'
    required: true
  end:
    type: str
    description: 'End: same format as start'
    required: true
  calendar_id:
    type: str
    description: Target calendar id; defaults to the primary calendar
    required: false
  description:
    type: str
    description: Event description
    required: false
  location:
    type: str
    description: Event location
    required: false
  all_day:
    type: bool
    description: If true, an all-day event (start/end are dates)
    required: false
  timezone:
    type: str
    description: IANA timezone for timed events; defaults to the user's configured timezone
    required: false
tags:
- calendar
- write
- create
aliases:
- create calendar event
- add event
- schedule event
- new calendar event
- book time
parents:
- calendar
requires:
- google_calendar
---

Creates an event on a real calendar through the provider seam. Consent is gated
in the capability layer (`work_buddy.calendar.capabilities`) with
`consent_weight="high"`, so the prompt fires individually for every create — it
is never carried by a workflow grant. The prompt body is rendered from the actual
pending change (calendar name, title, start–end in `USER_TZ`) and escalates when
the target calendar is shared.
