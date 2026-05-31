---
name: Calendar Delete Event
kind: capability
description: Permanently delete an event from one of the user's real calendars. Heavy per-change consent; always re-prompts because deletion is irreversible.
capability_name: delete_calendar_event
category: calendar
op: op.wb.calendar_delete_event
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
  notify:
    type: bool
    description: If true, notify attendees of the cancellation
    required: false
tags:
- calendar
- write
- delete
aliases:
- delete calendar event
- remove event
- cancel event
- delete meeting
parents:
- calendar
requires:
- calendar
---

Deletes an event through the provider seam. Consent is gated in the capability
layer with `consent_weight="high"` and `default_ttl=0`, so it re-prompts on every
call — deletion is irreversible. The prompt names the event being deleted (title
+ time in `USER_TZ`) and escalates when the target calendar is shared.
