---
name: Task Briefing Directions
kind: directions
description: How to present the daily task status summary — concise format, required sections, one next action
summary: 'Run task_briefing and present under 15 lines: focused tasks, overdue, stale alerts, inbox count, archive recommendation. End with ONE recommended next action.'
trigger: user wants a daily task status summary
command: wb-task-briefing
capabilities:
- tasks/task_briefing
tags:
- tasks
- briefing
- daily
- summary
- directions
aliases:
- task briefing
- daily task summary
- task status
parents:
- tasks
---

Run mcp__work-buddy__wb_run("task_briefing").

## Presentation format

Present concisely (under 15 lines):
1. Focused tasks -- list them (or note "none focused")
2. Overdue -- count and list if any
3. Stale alerts -- inbox needing triage, forgotten snoozed tasks
4. Inbox count -- how many waiting
5. Archive recommendation -- if >10 completed tasks clog the master list

End with ONE recommended next action.

## Don'ts
- Do NOT list every task
- Do NOT recommend process changes
- Keep it short -- under 15 lines total
