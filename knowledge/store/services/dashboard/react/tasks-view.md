---
name: React Tasks View
kind: system
description: Authoritative React task workspace for capture, filtering, triage, detail editing, Co-work knowledge, contextual Help, confirmed completion, and conflict recovery.
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

  Tasks uses the standard customizable dashboard grid and its persisted personalization. Keep view, slot, and widget instance IDs stable so saved placements, sizes, and additions survive upgrades. Browsing and detail are modes inside the workspace widget; neither bypasses Dashboard Core's Arrange/Preview modes or measured widget width. Workspace browse reads use work_buddy.tasks.workspace_query with SQL filtering, sorting, pagination, and self-excluding facets in one collection-revision snapshot. Only a selected task loads full aggregate/detail data; list rows do not hydrate documents. HttpTasksProvider ignores superseded query responses and retains prior results on refresh failure.

  TaskCompletionDialog shares React Aria's ModalOverlay, Modal, and Dialog primitives across browse and detail. Opening it freezes the saved task identity and revision; one client_mutation_id survives uncertain retries. TaskHelp uses Dashboard Core HelpTarget and native button titles to keep explanations attached to the controls, including compact layouts.

  Local-file rows are opaque handles. Never add an absolute path to browser contracts, logs, DOM text, or error strings. Refresh/reprobe goes through the same-origin host boundary. Focused Vitest coverage includes accessibility, responsive navigation, draft preservation, triage rotation, conflict handling, and linked-file actions. Persisted browser checks use the Tasks disposable live harness described in dev/testing/react-dashboard.
---

# React Tasks view

`/app/tasks` is the primary task surface. The old root-dashboard Tasks route redirects here instead of mounting a second task editor. Task traffic stays on same-origin `/api/tasks`; proposal traffic uses the protected Threads action-proposal API. Flask is the authority boundary to `TaskStore`, `TaskService`, Threads, Co-work, consent, and host-only local-file actions.

## Capture and authoring

Quick Add is directly visible while browsing; dedicated task detail offers a compact Quick capture disclosure that preserves the draft. Its default card is five grid rows tall so the initial controls fit; saved custom sizes remain authoritative and smaller cards retain content scrolling. Quick capture supports title-first entry, while the expanded composer exposes urgency, zero or more linked projects, independent namespace tags, dates, outcome, next action, definition of done, dependencies, and initial knowledge. Project selections carry stable registry IDs. Multi-line paste creates a review table, detects duplicates, and lets the user edit or exclude rows before a batch create. Successful requests return native IDs, revisions, receipts, and document metadata—never task lines or note paths.

**AI help** opens Dashboard Core's shared assisted-draft dock. It uses the same conversation primitives as Co-work and fills the visible form; it never creates a task or submits a form. See `services/dashboard/react/assisted-drafts` for disclosure, field conflicts, conditional Undo, and host-owned draft identity.

**Save proposal** creates a durable Thread, not a TaskStore row. `/app/tasks?proposal=th-…` opens its review pane; opening, refreshing, copying, or revisiting the link never accepts it. The exact reviewed numeric proposal event fences revision, dismissal, and creation. Realized proposals hand off to `/app/tasks?task=t-…` using the structured task receipt, not model-authored URLs. Journal Quick Capture uses this same Thread ingress and review UI.

The composer retains its draft and exact pending ingress/revision request through uncertain responses. Once linked, its Create action accepts that proposal instead of issuing a second direct create. Unsubmitted edits must be saved to the proposal first. The review pane preserves local edits across remote revision conflicts and provides explicit discard/load-current recovery. Interrupted creation offers a safe retry of the same permanent proposal mutation key. The bounded `task_proposals_reconcile` maintenance capability recovers accepted intents and Journal realization acknowledgements without model calls or automatic approval.

The Tasks provider retains its last validated proposal projection across the proposal-to-task redirect so Quick Add can observe a decision made in the review pane. An unchanged draft clears only when its exact Thread, proposal event and saved field fingerprint match the realized canonical proposal. Later edits and dismissed source drafts remain. Minimal non-assistable terminal metadata suppresses stale decisions after a reload; it cannot authorize a mutation or a clear. Unknown linked state requires review, while an unresolved exact pending request remains safely replayable. **Use retained fields for a new draft** explicitly starts a new editing lifetime through the widget host's atomic replacement reset, without creating a task or retaining the previous assistance binding.

If a proposal carries standard task settings outside Quick Add's field set, Quick Add links to the full review instead of revising away those settings or accepting them unseen. The full review displays every additional parameter and preserves it when common fields are edited. An uncertain, already-recorded request can still be replayed exactly; replay never authorizes a different current revision.

## Workspace

**Customize view** retains the dashboard's standard grid controls for arranging
and resizing widgets, the Widgets catalog, Preview, Undo, and saved layouts.
Task browsing and detail occupy the same workspace widget, so switching between
them does not replace the view or its saved layout.

Browsing starts with **Status: Open**, sorted by **Date created, newest first**.
Open includes every attention state, including Snoozed; Completed, Archived,
and Trash are excluded by that visible status filter. Status describes lifecycle.
Attention describes Inbox, Most Important, Working on now, Active, Waiting, or
Snoozed. They are separate dimensions, not competing task-state tabs.

Status, Projects, Attention, and Urgency are checkbox selectors across the top
with removable selection pills. Due date and Knowledge are single-choice
filters. Values within one dimension match any selection; different dimensions
combine. Clearing a selector means any value, so clearing Status includes all
four lifecycle statuses. **Clear filters** removes every restriction, including
the default Open selection. Filter changes apply immediately; text search is
debounced. Previous results stay visible while updating, and failed refreshes
offer Retry without erasing the list.

Filter buttons and their selection pills share a subtle theme-aware color per
dimension. Browsing filters, sorting, direction and pagination use the host's
view-scoped `task-browse` saved state. A bare Tasks URL restores that state;
an explicit browsing query overrides it completely. The workspace header eraser
resets all browsing dimensions together, including branch and exact namespace
selections, to Open / Date created / newest first / first page. Task-field drafts
and the customizable grid layout retain their separate scopes.

The namespace rail has its own hierarchy search, checkbox selection, and counts.
Tree counts retain a 12-pixel inset from the usable scrolling edge, in addition
to the reserved native scrollbar gutter, including at the minimum pane width.
It shows namespaces matching all other active filters, retaining selected zero-count
paths and their ancestors. Namespace selection itself does not remove alternative
choices. Manage namespaces always uses the complete inventory. Branches start
collapsed; deliberate expansions are remembered and search reveals matching
ancestors temporarily. The shared Co-work divider starts at a compact width,
allows resizing down to 160 pixels without a percentage floor, saves deliberate
width changes, and resets on double-click. Show namespaces sits at
the left of the results toolbar; Manage namespaces is inside the open pane only.
Legacy empty path segments have an explicit label, while filtering, saves, and Undo
preserve their exact stored spelling until a reviewed organization operation changes it.
Each namespace checkbox cycles blank → checkmark (including descendants) →
filled box (direct assignments only) → blank using a click or Space. Hover help
explains the cycle and accessible descriptions announce the current scope.
The rail's eraser clears every namespace selection together while retaining
other filters and sorting; the whole-card eraser encompasses that reset.
**No namespace** and **No project** are explicit choices.
Hiding the rail gives its width to task results; selected namespace pills remain
visible and effective. Narrow layouts start with the rail hidden. Namespace and
project filters are independent, and unresolved project associations remain
visible rather than being counted as No project.

Sorting has its own result-toolbar group, separate from filters. **Date created**
and **Date updated** default to newest first, **Title** to A–Z, **Due date** to
earliest first, and **Urgency** to highest first. The adjacent direction toggle
reverses the selected sort. Sorting applies to the whole matching collection
before pagination, with stable task-ID ties; missing dates stay last in either
direction. Rows always show creation dates and show updated dates when sorting
by Date updated. Unknown imported timestamps are labeled as unknown.

Selecting a task opens its full detail workspace instead of an empty side pane.
**Back to tasks** restores the browsing filters, sort, page, scroll, and focus on
the originating task where available. Task and proposal links are URL-addressable;
older lens/state bookmarks are translated into visible filters. **Triage inbox**
sets Open and Inbox explicitly, then shows at most five candidates with Most
Important, Working on now, Snooze, Archive, and local-only **Skip this pass**.
The visible filters still define the triage collection.

Detail edits structured fields and action items, independently edits project
links and namespaces, opens the task's Co-work document, and presents linked
local files through opaque host actions. Historical unresolved project links
are retained until the user chooses their replacements or explicitly removes
them. Complete/reopen, archive/unarchive, soft delete, restore, and delete undo
are revision-aware. Stale edits show a conflict with authoritative state.

The row checkmark and detail **Complete** button open **Complete this task?**
before changing anything. This alert dialog names the saved task and explains
that completion removes it from an Open-only list. **Cancel** receives initial
focus; Cancel or Escape closes the dialog and restores focus to the originating
control. The primary **Mark complete** action submits the reviewed task revision.
Unsaved field edits remain in the draft and are not included in completion.
While completion is pending, repeated submissions and dismissal are disabled.
An uncertain response offers **Retry completion** with the same request identity
and revision; a conflict requires canceling and reviewing the latest task.
To reverse completion later, include Completed in Status and choose **Reopen**.

Dashboard Core's **Help** mode provides contextual explanations across task
selection, lifecycle actions, filters, sorting, project and namespace fields,
triage, proposals, action items, and namespace organization. The explanations
identify whether a control only changes browsing, edits a draft, opens a review,
or writes immediately, with the relevant recovery action. In particular, the
checkmark opens
completion confirmation. Buttons also expose a brief native hover title. Reading
Help does not perform the explained action.

## Namespace organization

**Manage namespaces** opens a dedicated organization workspace. Select source
branches, then choose Rename, Move, Merge, Move children up one level, or Remove
assignments. It starts from the hierarchy and intended action, without assuming
a particular prefix transformation. To flatten `projects/`, Move children up one level
lifts its children to root; direct assignments on `projects` require an explicit
keep-or-move choice. Existing destination branches require reviewed merge
handling. Namespace operations never change project associations.

Task namespaces are editable pills on a dedicated, consistently aligned row.
Add namespace opens a searchable picker with existing paths and explicit path
creation. Removing a saved pill opens a confirmation scoped to that task; it
does not delete the namespace globally. An unchecked opt-in can skip further
namespace-removal confirmations for that task for ten minutes in the current
browser session. It starts only after a confirmed successful removal, never
extends on skipped removals, and can be revoked using **Confirmations off**.
Completion still confirms every time. Namespace changes save separately and
preserve unrelated task-field drafts. The dashboard has no bulk task namespace
replacement interface. Structural branch operations remain in Manage namespaces.
See `tasks/namespace-organization` for the API and conflict contract.

## Workspace query contract

`GET /api/tasks/view` accepts repeated `statuses`, `projects`, `namespaces`,
`exact_namespaces`, `attention`, and `urgencies` parameters, plus `q`, `due`,
`note`, `sort`, `direction`, `offset`, and `limit` (default 50, maximum 200).
An omitted status defaults to Open; an explicit empty `statuses` removes that
restriction. Project values are registry ID strings, `__none__`,
`__unresolved__`, or `unresolved:<historical-value>`. Namespace `__none__`
matches tasks with no namespace assignments. Search matches title and tags.

The response contains lightweight `tasks`, full-match `total`, `page`, `facets`,
`namespace_tree`, `options`, and `collection_revision`; `selected_task` supplies
detail only when requested. Facet counts omit their own filter dimension so
alternatives remain discoverable. This endpoint still runs through Python and
SQLite; performance comes from bounded hydration and database-side queries.

## Interaction contract

Read-only mode disables mutations. Browsing and detail occupy separate visible modes; dialogs trap and restore focus, status changes are announced, and skipping restores keyboard focus to the next candidate. In-flight drafts survive unrelated authoritative rerenders, while successful saves remount from the new revision.
