# Task Workspace UX review

## Follow-up acceptance corrections

The second user test exposed excessive namespace width, a separate scope link,
unclear reset coverage, and missing persisted browsing preferences. The latest
pass replaces the scope link with a keyboard-operable three-state checkbox,
adds a namespace-only eraser, and uses the standard card eraser to reset every
browsing filter (including namespaces), sorting and pagination. The latter
restores Open / Date created / newest first; task-field drafts and grid settings
remain separate. Explicit browse URLs override saved preferences completely.

The namespace pane now has a compact default and a 160px minimum without a
percentage floor. Deep indentation compresses at narrow pane widths. Filter
buttons and their corresponding pills share restrained theme-aware colors.
Assigned namespace pills have a larger text inset and retain a visible removal
control when their labels wrap.

The subsequent count-spacing correction increases the tree's scrollbar-side
inset from 4px to 12px, in addition to the reserved native scrollbar gutter.
Geometry checks measure the actual count-to-usable-edge gap in Chromium and
Firefox, including an overflowing tree and the 160px pane with a deep path.
The completed production run includes this correction: typecheck, build and
all 14 journeys passed, with successful isolated cleanup. The count gutter and
minimum-width screenshots were inspected for spacing and label clipping.

Quick Add's default height increases by one grid row so its bottom controls fit.
The default Workspace position moves down by the same row; saved user sizes and
placements remain respected. Both browser engines check the initial controls
against the card's content bounds.

Namespace removal offers an unchecked, task-specific ten-minute opt-in to skip
subsequent removal confirmations. It starts only after an accepted confirmed
removal, is shared between list and detail in that browser session, and can be
revoked. Completion still always confirms. Interrupted responses retain their
reviewed request identity for retry while that editor remains mounted; leaving
the entire detail before a response can lose its local retry dialog. This pass
does not introduce a durable mutation queue.

Independent source review caught a checkbox flex-width specificity conflict,
deep-label space pressure, optimistic query rollback on navigation failure, and
premature preference loss on reset failure. Those paths were corrected before
the browser acceptance run. Header reset confirmation freezes its draft scope
and revision, so navigating into a task during confirmation cannot clear that
task's field draft instead.

The final affected component, provider, persistence and host batch passed all
199 tests across 12 files. This includes rejected and thrown navigation/reset
responses, saved preferences, reviewed reset scope, task-specific cooldown,
completion confirmation, and preservation of custom grid sizes.

The final production typecheck and build passed, followed by **14/14 live
journeys**: thirteen in Chromium and the default/filter/sort smoke in Firefox.
The runner returned `ok: true` and `cleanup_succeeded: true`. New journeys cover
the three-state checkbox, namespace-only eraser, saved browsing state, explicit
URL precedence, whole-card reset cancellation/confirmation, and cooldown
isolation, navigation, reload and interrupted-response retry. Existing grid,
completion, organizer/Undo, task drafts and narrow-layout journeys also passed.

Twenty-three screenshots from that final run are retained below. Independent
visual review found no material clipping or overflow in the compact namespace
pane, default Quick Add, reset/cooldown dialogs or wrapped long namespace pill.
Earlier attempts exposed stale checkbox selectors and a search-settlement race
in the new narrow assertion; those test assumptions were corrected before the
successful full run. Screenshot review also caught insufficient pill padding and
Quick Add clipping, both fixed before the final build.

## Scope and verification status

This review covers task browsing, filtering, sorting, namespace assignments and
organization, project links, capture, completion, contextual Help, saved grid
layouts, and dedicated task details. All live domain writes use isolated
synthetic tasks and project records, with the authenticated session and matching
origin checked first. The normal dashboard and personal task stores are outside
this review.

**The earlier accepted baseline passed 11 production-bundle journeys, and two
focused journeys passed after its detail overlay guard.** That full run covered ten Chromium
journeys and a Firefox default/filter/sort smoke journey. After the final
three-line detail overlay guard, typecheck, the production build and both focused
namespace/completion journeys passed. Each runner reported
`cleanup_succeeded: true`. The focused checks held real namespace PATCH and
completion POST requests: Escape retained the dialog, details and draft while
the write was pending, and the operation succeeded after release. The full
affected component suites also passed all 55 tests.

## Current design and user goals

| Situation | Intended outcome and success condition | Boundaries and evidence |
| --- | --- | --- |
| Open Tasks | Show all nonterminal tasks, newest created first, with creation dates visible | Open includes inbox, active, focused, waiting and snoozed tasks; completed, archived and trashed tasks are excluded by default. The current Chromium journey, Firefox smoke and backend query regressions passed. |
| Narrow a large collection | Apply top-row filters immediately, with OR within a filter and AND between filters | Status and Attention are distinct mechanisms. Project and namespace filters are independent. Selected zero-result values remain removable; query regressions cover empty selections and boundary combinations. |
| Find a namespace or reclaim space | Search the hierarchy, expand a branch, resize the shared side panel, or hide it and return its width to results | The filter tree is contextual; assignment pickers use the complete namespace inventory. An empty contextual tree does not fall back to global picker options. Manual desktop resize, hide/show and focus checks passed; the empty-tree component regression passed. |
| Change ordering | Choose Date created, Date updated, Title, Due date or Urgency separately from the filters, with an adjacent direction toggle | Filtering and sorting precede paging; missing dates stay last in both directions and ties are deterministic. The current production journeys and query regressions passed. |
| Read or edit one task | Open dedicated details; Back restores browsing filters, sort, focus and scroll without an empty detail pane occupying browse space | Drafts survive navigation and reload. External revisions require deliberate reconciliation. The current production draft journey and component regressions passed; the final focused production check also passed busy-overlay Escape handling. |
| Change one task's namespaces | Use Add first, followed by fully rounded namespace pills; choose an existing path or explicitly create one, and confirm a saved pill's X before removal | The write affects this task's assignments only. Detail saves send namespaces and the reviewed revision immediately, preserve unrelated field drafts, and rebase their revision after acknowledgement. Current production coverage passed; manual checks confirmed persisted Add, Cancel, and successful removal after a narrow breakpoint. The final focused production check passed busy-overlay Escape handling. |
| Link a task to projects | Link multiple registered projects independently of namespace assignments | Historical unresolved links remain explicit. Namespace edits and reorganization preserve task and project identities. Earlier live cross-project capture and project/API regressions passed. |
| Arrange the dashboard | Retain standard widget chrome, saved placements and sizes, Add/remove widgets, resize, Arrange, Preview, Cancel and Save/reload | The redesign uses the existing grid contract. The current production grid journey, manual resize/Preview/Cancel checks and focused host/customization tests passed. |
| Capture while browsing | Use the Quick Add form directly in its existing card | Only dedicated detail mode uses compact capture disclosure. Saved card heights and positions remain respected; the current production capture journey and component checks passed. |
| Complete or recover a task | Open a named confirmation from the row checkmark or details; Cancel starts focused and completion requires Mark complete | Escape and Enter on Cancel preserve the task; uncertain retries retain the reviewed task and mutation ID. The current production completion/recovery journeys and component checks passed. The final focused rerun passed with the overlay event guard. |
| Reorganize namespaces | Start from general inventory, select paths and an operation, then review mapping, affected tasks and collisions before Apply | Rename, move, merge, promotion and assignment removal use explicit scope and atomic operations across lifecycle statuses. Bulk task selection and its Replace namespaces UI have been removed. The current production organizer journey and domain regressions passed. |
| Recover a namespace operation | Reload and Undo using the durable operation receipt | Revision fences reject intervening changes; domain tests cover stale previews, failed writes, retries and atomic recovery. The current production recovery journey passed. |
| Browse and edit across screen widths | Keep controls reachable, reclaim the namespace panel's width, and preserve open dialogs and drafts when the host changes responsive layout | Browsing density is evaluated after scrolling Task Workspace into view. The current production narrow journey passed; manual namespace removal retained its dialog and focused Cancel across the breakpoint. The separate busy-overlay Escape check passed on the rebuilt final source. |

## Corrections prompted by acceptance and review

The first redesign bypassed the standard grid and hid Customize view. User
acceptance testing identified that regression. The bypass was removed: all
existing layout controls, saved placements, sizes, widget chrome and Preview
behavior remain available. Quick Add again presents its usable form in browse
mode instead of leaving a collapsed button inside an otherwise reserved card.

The default page contains both Quick Add and Task Workspace. There is no promise
of two complete task rows in the initial page viewport: user-customized card
positions and heights determine what appears there. Task browsing density is
checked after scrolling the Workspace card into view. Initial-page screenshots
and Workspace-focused screenshots serve different purposes.

The namespace rail now uses the shared resizable side panel, starts with
collapsed branches, preserves hierarchy search and selected-zero recovery, and
reclaims its width when hidden. Contextual filter rows and complete assignment
options are separate data contracts. A complete picker inventory must not
repopulate an intentionally empty contextual tree.

Per-task namespace controls replace the selected-task bulk Replace flow. Add
appears before the saved pills, which occupy their own aligned row. Existing
assignments use fully rounded pills with an X that opens a removal confirmation;
Cancel makes no change. Manual additions and confirmed removals save immediately.
In details, these writes do not submit the title, project selection, ordinary
tags or other unsaved fields. Accepted receipts synchronize the namespace draft
and task revision while retaining those other edits. Uncertain retries keep the
original mutation ID and revision; conflicts require review before a new change.

The first corrective production run passed six journeys and exposed a completion
dialog disappearing across a responsive breakpoint. Manual testing then
demonstrated the same remount failure with a namespace dialog. The keepalive path
now also covers draft-owning widgets, preserving their live dialog and draft
state across responsive layout changes. Eighteen shared regressions and the
subsequent full 11-journey production run passed. Manual namespace removal also
retained its dialog with Cancel focused after the narrow breakpoint and completed
successfully when confirmed.

Review after that run identified a separate busy-state event problem: Escape on
the namespace overlay could reach the detail view's Back handler while a write
was pending. A three-line guard at the TaskDetail ancestor now protects namespace
add/remove and completion portals. All 55 affected component tests passed. Two
focused production journeys passed after rebuilding the final source: real
namespace PATCH and completion POST requests were held while Escape was pressed,
the dialog/details/draft remained present, and both operations succeeded after
release. The full 11-journey suite preceded this guard; the focused final-source
run verifies the affected paths.

Other earlier verification corrections include viewport-aware filter popovers,
an indented namespace inventory, completion confirmation and contextual Help,
coalesced task invalidations, and preservation of consumed authentication
fragments during query navigation. Hovering Help or cancelling a review does not
perform a domain mutation.

AI namespace suggestions remain deferred. The staleness proposal describes one
shared task-scoped review workflow, accessible from task context and the namespace
picker, with durable suggestions and explicit acceptance. This PR adds neither
its worker nor launch controls. Semantic staleness review likewise remains a
proposal; there is no automatic completion.

## Verification record

### Earlier corrective evidence

- The first corrective production run passed six journeys and failed the
  completion-dialog breakpoint check. Manual testing confirmed the namespace
  variant; the shared keepalive correction is recorded above.
- Shared host/grid/draft verification after the keepalive change passed 18 tests.
- The full affected component suites, TaskWorkspace and TaskNamespacePills,
  passed **55/55 tests** on the final source. Coverage includes namespace-only
  detail saves, cancellation, retention of title/summary/project drafts,
  immutable retries after a refreshed revision, conflict reconciliation, empty
  contextual namespace results, and the integrated busy-overlay Escape guard.
- Manual inspection confirmed that adding an existing namespace persisted after
  reload and cancelling removal retained the assignment. An open namespace
  removal dialog survived the narrow breakpoint with Cancel focused; the actual
  removal also succeeded after confirmation.
- The full production runner passed **11 journeys**: ten Chromium journeys cover
  grid customization, capture, contextual namespace filtering, per-task pills,
  drafts, completion, organization, recovery and narrow layouts; Firefox passed
  the default/filter/sort smoke journey. Typecheck and production build passed.
  The runner recorded `cleanup_succeeded: true`, and all 12 current screenshots
  were copied into this review's screenshot directory.
- After the three-line busy-overlay Escape guard, the rebuilt final source passed
  **two focused production journeys** for namespace pills and completion. The
  local Playwright log records `2 passed (1.3m)`; the runner exited zero with
  `ok: true` and `cleanup_succeeded: true`. Production typecheck/build also exited
  zero. Real namespace PATCH and completion POST requests were held to verify
  that Escape retained each dialog, task details and local draft, after which
  releasing the requests completed both operations successfully.

### Earlier regression and backend evidence

These checks remain useful within their stated scope but precede some acceptance
corrections. The full production result and final-source focused verification are
recorded separately above.

- Full Tasks Python suite: 285 passed after deferred project resolution and
  numeric-name corrections. Later focused compatibility checks passed 22
  project/receipt cases, 33 HTTP API cases and three aggregate integration cases.
- Query/event regressions: 39 passed. Namespace/API regressions: 41 passed.
  Focused project, proposal, assistance, rollback and migration checks also passed.
- Earlier component checks included 66 completion/Help cases, 43 button/Help
  cases, 60 provider cases and 12 route/security cases. Capture, namespace,
  standard host/customization and isolated-harness checks also passed.
- Earlier full component verification exposed obsolete capture-visibility,
  hover-title and synchronous focus assumptions. The affected checks passed
  after their assertions were aligned with the actual behavior. Capture setup
  now targets the directly visible browse form; compact detail capture still
  requires disclosure.
- Earlier production-bundle runs passed seven journeys in Chromium/Firefox and
  completed isolated teardown. These runs predate the grid and per-task namespace
  acceptance corrections.
- Knowledge validation covered 524 units with zero blocking errors; isolated
  BM25 rebuild and retrieval checks passed. The deployed knowledge store and its
  search index were not modified.

Disposable backend measurements are not browser latency guarantees. On a
6,000-task fixture, the warm default query median was about 375 ms versus
2,799 ms for the previous read shape. A separate synthetic outbox run delivered
and acknowledged 6,000 pending events as one notification in 744 ms; the previous
bounded loop took 8,004 ms for 100 notifications and left 5,900 pending. The
profiling scripts are retained under `scripts/profile_task_*.py`.

### Visual evidence and review limitations

Current screenshots from the final fourteen-journey production run show
[desktop default page](task-workspace/desktop-page-default.png),
[narrow default page](task-workspace/narrow-page-default.png),
[desktop browsing](task-workspace/desktop-browse.png),
[narrow browsing](task-workspace/narrow-browse.png),
[customized task layout](task-workspace/customized-task-layout.png),
[resized namespace panel](task-workspace/resized-namespace-pane.png),
[namespace pills in details](task-workspace/task-namespace-pills-detail.png),
[completion confirmation](task-workspace/completion-confirmation.png),
[completion Help](task-workspace/completion-help.png),
[narrow project selection](task-workspace/narrow-project-picker.png),
[namespace inventory](task-workspace/namespace-inventory.png), and
[namespace preview](task-workspace/namespace-preview.png). The latest corrections
also have captures of the
[collapsed namespace pane](task-workspace/namespace-collapsed-default-width.png),
[160px minimum width](task-workspace/namespace-minimum-width.png),
[count spacing beside the scrollbar](task-workspace/namespace-count-scrollbar-gutter.png),
[branch checkbox](task-workspace/namespace-branch-checkbox.png),
[exact checkbox](task-workspace/namespace-exact-checkbox.png),
[desktop pill padding](task-workspace/namespace-pill-padding-desktop.png),
[narrow pill wrapping](task-workspace/namespace-pill-wrapping-narrow.png),
[cooldown opt-in](task-workspace/namespace-cooldown-opt-in.png),
[interrupted-response retry](task-workspace/namespace-cooldown-safe-retry.png),
[restored browsing state](task-workspace/task-browse-state-restored.png), and
[whole-card reset confirmation](task-workspace/task-browse-reset-confirmation.png).
These 23 captures use synthetic data. Busy-overlay keyboard behavior is
established by the passing interaction checks, rather than static images.

The independent reviewer inspected screenshots and raised viewport-density and
inventory-hierarchy issues. This was a design-hypothesis review with partial prior
source exposure, not an independent live walkthrough: the reviewer's browser
tools exposed no browser. The parent performed the live interface checks. An
unresponsive Playwright MCP browser was a tool failure; inspection continued in
the in-app browser, while automated domain mutations used the isolated runner.

No personal namespace cleanup is part of this change. Read-only provenance
inspection established that the reported malformed namespaces predated
deployment; existing exact paths remain recoverable rather than being silently
rewritten. Project ambiguities remain explicit until the user resolves them.

Live AI generation and every retained Co-work document lifecycle operation were
not re-exercised in this review. Full assistive-technology testing remains
untested; changed-flow keyboard focus and accessible control semantics have
component and live evidence as described above.
