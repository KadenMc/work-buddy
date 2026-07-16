---
name: Day Planner Plugin Integration
kind: integration
description: Obsidian Day Planner plugin (v0.28.0) integration -- plan entry format, settings, runtime surface
tags:
- obsidian
- day-planner
- scheduling
- eval_js
- time-blocking
aliases:
- day planner plugin
- time blocks plugin
- obsidian day planner
parents:
- obsidian
- obsidian
---

Day Planner plugin integration via eval_js bridge.

## Architecture

Plan entries are ephemeral scheduling artifacts -- NOT canonical tasks. Source of truth for tasks = master-task-list.md + SQLite store. Day Planner entries describe *when* to work; the task system describes *what*.

## Plan Entry Format

Section heading must be exactly `# Day planner` (case-sensitive, H1).

```
- [ ] 09:00 - 10:30 Deep work: ECG-FM paper writing
- [ ] 10:30 - 10:45 Break
- [ ] 10:45 - 11:30 [Cal] Lab meeting
```

Timestamps: 24-hour HH:mm. Calendar events prefixed with [Cal]. Tasks snap to 10-minute intervals.

## Key Settings

plannerHeading: "Day planner", plannerHeadingLevel: 1, timestampFormat: "HH:mm", defaultDurationMinutes: 30, snapStepMinutes: 10, startHour: 6.

## Plugin Runtime Surface

Redux store with slices: obsidian (UI state, icalEvents, layoutReady), search, dataview, settings, ical (remote calendars). Commands: re-sync, show-day-planner-timeline, show-weekly-view, show-multi-day-view, clock-in, clock-out. Also exposes: store (Redux), dataviewFacade, vaultFacade, transationWriter, sTaskEditor, settingsStore.

## Python API

check_ready (returns settings), get_todays_plan, write_plan (consent-gated), trigger_resync, generate_plan (pure logic from calendar + tasks).

### generate_plan input shapes

**calendar_events** accepts either of:
- Flat: `{start: "HH:MM"|ISO, end: "HH:MM"|ISO, summary|description|text: "...", past?: bool}`
- Google Calendar API: `{start: {dateTime: ISO}, end: {dateTime: ISO}, summary, timeStatus}` (all-day and timeStatus=="past" are skipped)

**focused_tasks** dicts accept:
- `description` or `text` (required) — label
- `duration` (optional int minutes) — overrides config default_task_duration
- `time_start` (optional "HH:MM") — pin to that start; conflicts fall to unscheduled

**clamp_to_now** config (default True) clamps the effective work start to current local time so unpinned tasks are never placed in the past. Pinned tasks (time_start) bypass the clamp — set deliberately for retrospective blocks.

"Current local time" means the configured Work Buddy timezone, not the host process's incidental timezone. Journal-bound reads and writes use the shared logical-day authority from `journal/day-lifecycle`, including its boundary and DST resolution.
