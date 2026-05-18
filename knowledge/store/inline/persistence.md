---
name: Inline Persistent Watchers
kind: concept
description: 'How #wb/cmd/* tags declared persistent install a PersistentWatcher that survives Obsidian restarts'
summary: Handlers with persistent=True register a PersistentWatcher row on first detection instead of running. The inline-sync sidecar job reconciles vault tags ↔ watcher store every 10 min, firing due watchers and cancelling orphans.
tags:
- inline
- persistence
- watcher
- sync
aliases:
- inline watcher
- persistent tag
- recurring tag
parents:
- inline
- inline
---

# Persistent watchers

When a handler declares `persistent=True`, a detected `#wb/cmd/<name>` tag does NOT invoke the handler immediately. Instead, the dispatcher registers a `PersistentWatcher` row in `data/agents/inline.db`.

## Reconciliation (`sidecar_jobs/inline-sync.md`)

Runs every 10 min via the sidecar, calls `inline_sync`:

1. For each persistent command, scan the vault for files containing its tag via `work_buddy.obsidian.tags.search_by_tag(..., mode="prefix")`.
2. For each vault-present tag not in the store → create a watcher (heals drift if live detection missed one).
3. For each stored watcher whose tag is no longer in its file → delete the watcher.
4. For each enabled watcher that is due per its schedule → enqueue for execution.

This is the same vault-is-canonical discipline used by `sidecar_jobs/task-sync.md`.

## Live cancellation

The Obsidian plugin POSTs to `/inline/tag-removed` when `metadataCache.changed` shows a previously-tracked tag is gone, so watchers are cancelled immediately (the sync job catches anything the live path misses).

## Schema (`inline_watchers` table)

- `watcher_id` (pk)
- `command_name`
- `file_path`, `tag`, `tag_line`
- `params` (JSON)
- `schedule` (cron str or null)
- `created_at`, `last_run_at`
- `enabled`
