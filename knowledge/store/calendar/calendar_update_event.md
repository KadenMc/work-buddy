---
name: Calendar Update Event
kind: capability
description: Modify an existing event on one of the user's real calendars. Heavy per-change consent with a before→after diff in the prompt; always re-prompts because an update can cancel or move an event.
capability_name: update_calendar_event
category: calendar
op: op.wb.calendar_update_event
schema_version: wb-capability/v1
mutates_state: true
consent_required: true
parameters:
  calendar_id:
    type: str
    description: Calendar id the event lives on
    required: true
  event_id:
    type: str
    description: Provider-local event id
    required: true
  changes:
    type: dict
    description: 'Partial fields to change: any of summary, start, end, location, description'
    required: true
  notify:
    type: bool
    description: If true, notify attendees of the change
    required: false
tags:
- calendar
- write
- update
aliases:
- update calendar event
- edit event
- move event
- reschedule event
- change event
parents:
- calendar
requires:
- google_calendar
---

Modifies an event through the provider seam. Consent is gated in the capability
layer with `consent_weight="high"` and `default_ttl=0`, so it re-prompts on
every call (an update can cancel an event or move it across days). The prompt
renders a before→after diff of the changed fields (title, time, location) in
`USER_TZ` and escalates when the target calendar is shared.
