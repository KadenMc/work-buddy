---
name: Day Planner
kind: capability
description: 'Generate a time-blocked plan from calendar events and focused tasks, then optionally store it as a Source-backed native Journal artifact.'
capability_name: day_planner
category: journal
op: op.wb.day_planner
schema_version: wb-capability/v1
parameters:
  action:
    type: str
    description: '''status'', ''read'', ''generate'', ''write'', or ''generate_and_write'''
    required: true
  target:
    type: str
    description: 'Date target for read/write. Default: today.'
    required: false
  calendar_events:
    type: str
    description: 'For generate: JSON list of events. Flat shape {start: ''HH:MM'', end: ''HH:MM'', summary/description/text: ''...'', past?: bool} OR Google Calendar API shape {start: {dateTime: ISO}, end: {dateTime: ISO}, summary, timeStatus}. See wrapper docstring for full spec.'
    required: false
  focused_tasks:
    type: str
    description: 'For generate: JSON list of task dicts. Required: ''description'' or ''text''. Optional: ''duration'' (int minutes, overrides config default), ''time_start'' (''HH:MM'' — pins task to that time; goes unscheduled on conflict).'
    required: false
  config_overrides:
    type: str
    description: JSON dict of day_planner config overrides (work_hours, default_task_duration, break_interval, clamp_to_now — default True, prevents placement in the past).
    required: false
  client_mutation_id:
    type: str
    description: Stable idempotency key for write or generate_and_write. Reuse only for the exact same plan.
    required: false
mutates_state: true
retry_policy: manual
tags:
- journal
- day
- planner
aliases:
- day planner
- time blocking
- schedule
- daily plan
- time blocks
parents:
- journal
requires: []
---

The scheduling algorithm is pure and provider-neutral. `status` checks native
Journal authority and the active profile, `read` returns the latest native
`wb.journal-day-plan/v1` artifact for the logical day, and write actions commit
the exact generated payload to Sources before creating the Journal item. No
Obsidian plugin, daily file, resync command, or remote-calendar projection is
used.
