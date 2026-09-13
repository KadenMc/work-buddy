---
name: Task Sync
kind: skill
description: Retired legacy Markdown reconciliation surface; unavailable under native task authority.
category: tasks
op: op.wb.task_sync
schema_version: wb-skill/v1
skill_name: task_sync
tags:
- tasks
- task
- sync
aliases:
- sync tasks
- reconcile tasks
- task discrepancy
- task watcher
parents:
- tasks
---

Do not call this skill after native cutover. `TaskStore` is already
canonical, and `sidecar_jobs/task-sync.md` is disabled indefinitely. The frozen
legacy files are import and rollback evidence, not a reconciliation peer.
