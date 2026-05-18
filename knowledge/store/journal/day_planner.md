---
name: Day Planner
kind: capability
description: 'Day Planner operations: check plugin status, read current plan, generate schedule from events+tasks, or write plan to journal. Composite: replaces separate check_ready/get_plan/generate/write/resync calls.'
capability_name: day_planner
category: journal
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
- journal
requires:
- obsidian
---
