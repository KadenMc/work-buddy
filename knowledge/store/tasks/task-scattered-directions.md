---
name: Scattered Tasks Directions
kind: directions
description: How to present scattered task results and triage into action categories
summary: 'Present results grouped by file. Assess: stale tasks, active project tasks, duplicates. Suggest concrete actions. Keep output concise.'
trigger: user asks to find tasks scattered across the vault
command: wb-task-scattered
capabilities:
- tasks/task_scattered
tags:
- tasks
- scattered
- triage
- directions
aliases:
- find scattered tasks
- orphan tasks
- tasks outside master list
parents:
- tasks
---

Run mcp__work-buddy__wb_run("task_scattered").

Present results grouped by file. For each:
- File path and task count
- First few task descriptions (truncated)
- Any project tags

Then assess:
1. Stale tasks -- in old journal entries, never migrated
2. Active project tasks -- in project docs, current but untracked
3. Duplicates -- appear in multiple locations

Suggest:
- Which to migrate to master list?
- Which are stale and should close?
- Which are project-specific and fine where they are?

Keep concise -- quick scan, not a report.
