---
name: React Tasks View
kind: system
description: Authoritative React task workspace for capture, batch authoring, filtering, triage, detail editing, Co-work knowledge, local-file links, lifecycle, and conflict recovery.
summary: /app/tasks is the native task UI; it uses same-origin APIs and revision-aware intents and never renders or edits Obsidian task Markdown.
tags:
- dashboard
- react
- tasks
- cowork
- accessibility
aliases:
- React Tasks tab
- task workspace
- /app/tasks
parents:
- services/dashboard/react
entry_points:
- dashboard-react/src/apps/tasks
- work_buddy.dashboard.tasks_api
dev_notes: |-
  The Tasks app contributes its view, widgets, schemas, and provider through the dashboard registry. Widget mutations receive a stable `client_mutation_id`; providers preserve in-flight composer/detail drafts across authoritative refreshes and surface revision conflicts with fresh server state.

  Local-file rows are opaque handles. Never add an absolute path to browser contracts, logs, DOM text, or error strings. Refresh/reprobe goes through the same-origin host boundary. Focused Vitest coverage includes accessibility, responsive inert panes, draft preservation, triage rotation, conflict handling, and linked-file actions.
---

# React Tasks view

`/app/tasks` is the primary task surface. The old root-dashboard Tasks route redirects here instead of mounting a second task editor. Browser traffic stays on same-origin `/api/tasks`; Flask is the authority boundary to `TaskStore`, `TaskService`, Co-work, consent, and host-only local-file actions.

## Capture and authoring

Quick capture supports title-first entry, while the expanded composer exposes urgency, project, namespace tags, dates, outcome, next action, definition of done, dependencies, and initial knowledge. Multi-line paste creates a review table, detects duplicates, and lets the user edit or exclude rows before a batch create. Successful requests return native IDs, revisions, receipts, and document metadata—never task lines or note paths.

## Workspace

Focused, Inbox, All active, Snoozed, Completed/Archived, Trash, and Inbox triage lenses share search and structured filters. The triage view shows at most five candidates and offers Most Important, Working on now, Snooze, Archive, and a local-only **Skip this pass** action that rotates the item without mutating task state.

The detail pane edits structured fields and action items, opens the task's Co-work document, and presents linked local files through opaque host actions. Complete/reopen, archive/unarchive, soft delete, restore, and delete undo are revision-aware. A stale edit returns a visible conflict and refreshes authoritative state instead of silently overwriting it.

## Interaction contract

Read-only mode disables mutations. Mobile list/detail panes use `hidden` plus `inert`; dialogs trap and restore focus; status changes are announced; skipping restores keyboard focus to the next candidate. In-flight drafts survive unrelated authoritative rerenders, while successful saves remount from the new revision.
