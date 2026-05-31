---
name: Calendar Get Event
kind: capability
description: Fetch one calendar event by its provider-local id on a given calendar. Uses a windowed lookup because the Obsidian plugin's getEvent is broken; prefer calendar_list_events with a tight range when you know the date.
capability_name: calendar_get_event
category: calendar
parameters:
  calendar_id:
    type: str
    description: Calendar id the event lives on
    required: true
  event_id:
    type: str
    description: Provider-local event id (from a prior list_events)
    required: true
op: op.wb.calendar_get_event
schema_version: wb-capability/v1
tags:
- calendar
- event
aliases:
- get calendar event
- fetch event
- look up event
- single event
parents:
- calendar
requires:
- calendar
---
