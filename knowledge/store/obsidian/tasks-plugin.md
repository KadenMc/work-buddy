---
name: Retired Obsidian Tasks Plugin Compatibility
kind: integration
description: Frozen pre-cutover Tasks-plugin integration retained for import, audit, and rollback evidence only; never live task authority.
tags:
- obsidian
- tasks
- plugin
- cache
- mutation
- ownership
- eval_js
- emoji-sync
- soft-delete
aliases:
- obsidian tasks
- tasks plugin
- task cache
- task mutation
parents:
- obsidian
- obsidian
---

# Retired task integration

This integration describes the pre-cutover Obsidian Tasks plugin (v7.23.1)
pipeline. After native task authority activates, Obsidian and the plugin are
disconnected from live task reads, writes, sync, notes, context, dashboards, and
secondary consumers. `TaskStore` and projection-free Co-work documents are the
only current authorities; see `tasks/native-task-system`.

The code and frozen files remain solely for backup verification, deterministic
import, audit, and explicitly invoked rollback work. They are retained
indefinitely. No scheduled process reconciles or deletes them.

## Historical architecture

## Architecture

Python wrappers in env.py execute JS snippets from _js/ via bridge.eval_js(). All data comes from plugin cache (plugin.cache.tasks), not disk parsing.

## Plugin API (apiV1)

executeToggleTaskDoneCommand(line, path) -> programmatic toggle, returns new line text. Handles recurring tasks, done dates, and status transitions. createTaskLineModal() and editTaskLineModal() open UI modals (not programmatic).

## Plugin-Native Mutation Pipeline

1. Find Task in cache by ID/description
2. Call task.handleNewStatus(newStatus) -> returns new Task[] (handles recurrence, dates, pure function)
3. Call result[0].toFileLineString() -> correctly formatted line string
4. Write string to file via bridge.write_file() or vault.process()
5. Cache auto-reindexes from file change via vault subscription

## Historical ownership split

Tasks plugin owns: checkbox state, done/cancelled dates, recurrence, priority emojis, due/scheduled/start dates.
work-buddy owns: state (inbox/mit/focused/snoozed/done/deleted), urgency, complexity, contract link, snooze-until, state change history, task ID (t-<hex>), soft-delete tombstone (deleted_at).
Shared: #projects/* tags.

This split no longer applies to native tasks.

## Historical emoji-aware import

The one-time legacy importer parses Tasks-plugin emoji metadata into native fields:

- deadline emoji + YYYY-MM-DD -> `task_metadata.deadline_date` + `has_deadline=True`
- urgency emojis (high / medium / low) -> `task_metadata.urgency`
- done emoji + YYYY-MM-DD -> `task_metadata.completed_at`

`task_sync` is retired and its scheduled job is disabled.

## Native lifecycle replacement

The native store uses reversible lifecycle fields across tasks and action items:

- task delete sets `deleted_at` and can be restored;
- archive sets `archived_at` and can be reversed;
- action-item delete retains the row and its sequence.

Restoring a retired task document creates a new active Co-work binding while
preserving retired document history.

## Priority Mapping

1=high (mapped from emoji), 2=medium (mapped from emoji), 3=none/default (no emoji).

## Task Object Key Properties

description, originalMarkdown, statusCharacter, status.type (TODO/DONE), priority (1-3), tags, _dueDate/_doneDate/_createdDate (Moment objects), recurrence, children, parent, taskLocation._tasksFile._path, taskLocation._lineNumber.

## Limitations

- Tasks plugin cache is read-only for programmatic use. Creation uses raw markdown approach.
- Cache includes all vault files -- filter by file_path for canonical list.
- Moment.js dates must be formatted before serialization.
- Task objects have circular parent/children refs preventing naive JSON.stringify.
