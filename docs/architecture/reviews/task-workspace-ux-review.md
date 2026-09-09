# Task Workspace UX review

## Scope

Task browsing, filtering, sorting, namespace organization, project associations,
capture, completion confirmation, contextual Help, and the transition between browsing and a dedicated task view. Verification
uses the isolated Tasks harness with synthetic tasks and project records. The
normal dashboard and personal task stores are outside this review.

## Scenarios and outcomes

| Situation and starting point | Intended outcome and success condition | Exceptional conditions | Evidence |
| --- | --- | --- | --- |
| Open Tasks without filters | See all nonterminal tasks, newest created first, with creation dates visible | Waiting, snoozed, inbox, focused and active tasks; completed/archived/trash excluded | Live-verified in Chromium; production browser regression |
| Narrow a large collection | Combine namespaces, projects, statuses and other top filters immediately; OR within each filter and AND between filters | Empty selection, no namespace, unresolved project, long labels, no matches | Live-verified core filters; Python query regressions cover boundary combinations |
| Recover browsing space | Hide namespace rail and reclaim its column while retaining selected pills | Mobile rail starts collapsed; branch selection includes descendants unless changed to exact | Live-verified desktop width gain and mobile show/hide; production browser regression |
| Change ordering | Select a sort field and reverse direction; order the complete matching collection before paging | Missing or invalid dates stay last in both directions; deterministic ties | Live-verified created/title sort and pagination; Python query regressions |
| Read or edit one task | Dedicated task view, then Back restores filters, sorting, focus and scroll; draft survives Back and reload | External revision, edits made during save, direct task link | Live-verified Back/reload/save; component tests cover external revision and save races |
| Capture a cross-project task | Link two registry projects and an unrelated namespace, then see all three on the saved task | New namespace confirmation, empty capture, retained draft | Live-verified create with two projects; component/API contracts cover validation |
| Understand and complete a task | Help explains the checkmark; browse and detail open a named confirmation; Cancel starts focused and returns focus | Escape/Enter cancel, pending double click, uncertain response and revision conflict, narrow viewport | Live-verified dialog/cancel/focus on both entry points; production mutation/recovery regression |
| Reorganize namespaces | Begin from general inventory, select a branch and an action, inspect mapping and affected tasks, then cancel or apply | Existing destination, direct parent assignments, all lifecycle statuses, duplicate membership | Live-verified preview/cancel/apply/Undo walkthrough; production browser regression |
| Recover a namespace change | Reload and Undo from a durable operation receipt | Later task edits, stale preview, failed second write, retry after delivery failure | Live-verified reload/Undo; domain tests cover atomic failure and stale revisions |
| Browse on a phone-sized screen | Multiple full tasks visible without an empty capture/details pane; controls remain reachable | Long title/project/namespace labels, popup near viewport edge, dark appearance | Live-verified at 390×844 with screenshots and keyboard Escape/focus checks |

## Changes made after verification

- Replaced Apply with immediate filter changes and visible selections; removed
  overlapping lens tabs. Status and attention have distinct, consistent labels.
- Replaced the empty split view with dedicated task/proposal views and Back.
- Added a hierarchy rail, separate sort controls, creation dates, and independent
  multi-project links.
- Made capture a compact disclosure and removed redundant card chrome in Tasks.
  The initial narrow viewport now shows multiple complete rows. Draft controls
  and Help remain available.
- Replaced a mobile popup whose static position could fall outside the viewport
  with a viewport-aware React Aria popover; Escape restores trigger focus.
- Changed namespace inventory from a row-major grid to an indented list, keeping
  action choices above the inventory and making parent/child relationships clear.
- Coalesced collection invalidation: a live task save caused two view requests
  after the fix, compared with roughly 100 during the initial backlog experiment.
- Prevented query navigation from restoring a consumed authentication fragment.
  The production Undo-after-reload journey exposed the stale router location;
  route/security regression tests cover ordinary and authenticated launch links.
- Added a completion confirmation using shared React Aria dialog primitives,
  together with Help for lifecycle, selection, filters, sorting, projects, and
  namespace operations. Hovering or cancelling does not perform a task mutation.

## Verification record

### Captured interfaces

Screenshots use synthetic data from the production-bundle journeys:
[desktop browsing](task-workspace/desktop-browse.png),
[narrow browsing](task-workspace/narrow-browse.png),
[completion confirmation](task-workspace/completion-confirmation.png),
[completion Help](task-workspace/completion-help.png),
[narrow project selection](task-workspace/narrow-project-picker.png),
[namespace inventory](task-workspace/namespace-inventory.png), and
[reviewed namespace mapping](task-workspace/namespace-preview.png).

### Live interface

The parent reviewed actual isolated React + Flask + SQLite behavior before encoding
the browser regressions. Authenticated session and matching origin were checked
before domain writes. Desktop and narrow browser exploration covered the flows
listed above. Mobile dark-mode browsing was also inspected. An additional general
rename preview was started but its settled result was not observed before teardown;
it is not counted as live evidence. Earlier screenshots are not used as evidence for
layouts that were subsequently changed.

The independent reviewer inspected screenshots and raised the initial viewport
density and inventory hierarchy issues; both were addressed. This was a
**design-hypothesis** review, not an independent live walkthrough: the reviewer's
browser tools exposed no browser, and they had partial prior source exposure.

The original Playwright MCP browser became unresponsive during inspection. The
parent continued visual and keyboard checks in the in-app browser. This tool
failure is not classified as a dashboard defect. Domain mutation regression
checks continue through the isolated Playwright runner, which verifies its own
authenticated session and origin.

### Automated and source evidence

- `uv run --no-sync python -m pytest tests/unit/tasks -q`: full Tasks suite passed,
  285 tests after deferred project resolution and numeric-name corrections.
- Later project/receipt and HTTP API suites passed 22 and 33 cases, including
  pre-upgrade receipt replay, registry changes, and rejection of changed requests.
  Three aggregate integration cases also passed: completed historical replay,
  rejection of modern-request reinterpretation, and ordinary new project creation.
- Current query/event regressions: 39 passed; namespace/API regressions: 41 passed.
- Project/proposal/assistance consumers, rollback and migration checks passed in
  focused integration suites.
- Tasks component suites, shared content-flow host tests, assistance contracts,
  and the live harness guard tests passed. Completion and Help refinements passed
  66 focused Tasks cases. Native button titles and additional Help coverage passed
  43 focused cases; the provider passed 60 and route/security passed 12.
- Knowledge store: 524 units validated with zero blocking errors; isolated BM25
  rebuild and retrieval checks passed.
- `npm run test:e2e:live -- --app tasks`: all seven journeys passed, including
  Chromium mutations and Firefox browsing; teardown succeeded. This runner
  builds production assets, exercises real isolated HTTP mutations, and records
  teardown success. A prior run passed browsing/filter/sort/Back checks and found
  an outdated organizer test locator; the locator was corrected to the observed
  accessible name without weakening the outcome assertions.
  The screenshot-readiness refinement also passed all seven journeys using the
  same production assets, with successful teardown.

Performance measurements are **source-inferred backend evidence**, not browser
latency guarantees. On a disposable 6,000-task fixture, warm default query median
was about 375 ms versus 2,799 ms for the previous read shape. A separate synthetic
outbox run delivered and acknowledged all 6,000 pending events as one notification
in 744 ms; the previous bounded loop took 8,004 ms for 100 notifications and left
5,900 pending. Both scripts are retained under `scripts/profile_task_*.py`.

## Remaining boundaries

No personal-data migration or actual namespace cleanup was performed. Actual
project ambiguities remain explicit until resolved by the user. Live AI-provider
generation and every Co-work document lifecycle operation were not re-exercised
in this Tasks review; existing component/API coverage remains the evidence for
those retained paths. Full assistive-technology testing is untested; keyboard
focus and accessible control semantics were checked on the changed flows.

Documentation validation and hygiene checks ran against the isolated worktree.
The deployed knowledge store and its search index were not modified.
