---
name: Inline Persistent Watchers (Retired)
kind: concept
description: Historical record of the retired #wb/cmd/* watcher store and reconciliation schedule.
summary: 'inline-sync is disabled. Watcher rows are retained only for audit/migration and never scanned, fired, healed, or recreated in the native profile.'
tags:
- inline
- persistence
- retired
- watcher
aliases:
- inline watcher
- persistent tag
parents:
- inline
---

The `sidecar_jobs/inline-sync.md` schedule is disabled and the public watcher
capability declarations are removed. Existing `inline.db` rows are inert
migration evidence. Do not scan the vault, enqueue watcher executions, cancel
rows based on file changes, or offer Obsidian setup advice.
