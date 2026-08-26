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

`/app/tasks` is the primary task surface. The old root-dashboard Tasks route redirects here instead of mounting a second task editor. Task traffic stays on same-origin `/api/tasks`; proposal traffic uses the protected Threads action-proposal API. Flask is the authority boundary to `TaskStore`, `TaskService`, Threads, Co-work, consent, and host-only local-file actions.

## Capture and authoring

Quick capture supports title-first entry, while the expanded composer exposes urgency, project, namespace tags, dates, outcome, next action, definition of done, dependencies, and initial knowledge. Multi-line paste creates a review table, detects duplicates, and lets the user edit or exclude rows before a batch create. Successful requests return native IDs, revisions, receipts, and document metadata—never task lines or note paths.

**AI help** opens Dashboard Core's shared assisted-draft dock. It uses the same conversation primitives as Co-work and fills the visible form; it never creates a task or submits a form. See `services/dashboard/react/assisted-drafts` for disclosure, field conflicts, conditional Undo, and host-owned draft identity.

**Save proposal** creates a durable Thread, not a TaskStore row. `/app/tasks?proposal=th-…` opens its review pane; opening, refreshing, copying, or revisiting the link never accepts it. The exact reviewed numeric proposal event fences revision, dismissal, and creation. Realized proposals hand off to `/app/tasks?task=t-…` using the structured task receipt, not model-authored URLs. Journal Quick Capture uses this same Thread ingress and review UI.

The composer retains its draft and exact pending ingress/revision request through uncertain responses. Once linked, its Create action accepts that proposal instead of issuing a second direct create. Unsubmitted edits must be saved to the proposal first. The review pane preserves local edits across remote revision conflicts and provides explicit discard/load-current recovery. Interrupted creation offers a safe retry of the same permanent proposal mutation key. The bounded `task_proposals_reconcile` maintenance capability recovers accepted intents and Journal realization acknowledgements without model calls or automatic approval.

The Tasks provider retains its last validated proposal projection across the proposal-to-task redirect so Quick Add can observe a decision made in the review pane. An unchanged draft clears only when its exact Thread, proposal event and saved field fingerprint match the realized canonical proposal. Later edits and dismissed source drafts remain. Minimal non-assistable terminal metadata suppresses stale decisions after a reload; it cannot authorize a mutation or a clear. Unknown linked state requires review, while an unresolved exact pending request remains safely replayable. **Use retained fields for a new draft** explicitly starts a new editing lifetime through the widget host's atomic replacement reset, without creating a task or retaining the previous assistance binding.

If a proposal carries standard task settings outside Quick Add's field set, Quick Add links to the full review instead of revising away those settings or accepting them unseen. The full review displays every additional parameter and preserves it when common fields are edited. An uncertain, already-recorded request can still be replayed exactly; replay never authorizes a different current revision.

## Workspace

Focused, Inbox, All active, Snoozed, Completed/Archived, Trash, and Inbox triage lenses share search and structured filters. The triage view shows at most five candidates and offers Most Important, Working on now, Snooze, Archive, and a local-only **Skip this pass** action that rotates the item without mutating task state.

The detail pane edits structured fields and action items, opens the task's Co-work document, and presents linked local files through opaque host actions. Complete/reopen, archive/unarchive, soft delete, restore, and delete undo are revision-aware. A stale edit returns a visible conflict and refreshes authoritative state instead of silently overwriting it.

## Interaction contract

Read-only mode disables mutations. Mobile list/detail panes use `hidden` plus `inert`; dialogs trap and restore focus; status changes are announced; skipping restores keyboard focus to the next candidate. In-flight drafts survive unrelated authoritative rerenders, while successful saves remount from the new revision.
