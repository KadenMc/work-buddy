---
name: Co-work Verify and Co-think surfaces
kind: system
description: HTTP, MCP, event, Review, Chat, and editor projections for Co-work Verify and Co-think.
summary: Compact Working on controls and sibling editor-bottom Verify/Co-think docks share exact document targeting. Verify configuration and execution stay in its dock; Review receives durable outputs, human decisions, and contextual recheck handoffs, while Chat inherits Working on through a compact About chip.
entry_points:
- work_buddy.cowork.verify_api
- work_buddy.cowork.verify_projection
- work_buddy.cowork.chat_api
- work_buddy.cowork.ops
- work_buddy.truth.events
- dashboard-react/src/apps/cowork/bridge/LiveReviewRailProvider.ts
- dashboard-react/src/apps/cowork/surface/CoworkWorkspaceSurface.tsx
tags:
- cowork
- verify
- co-think
- http
- mcp
- events
- review-rail
aliases:
- Verify API
- Co-think API
- Verify Review rail
- Verify run history
parents:
- cowork/verify-and-cothink
dev_notes: |-
  R2 additive fields are the common frontend projection. `LiveReviewRailProvider`
  publishes the same mapped read to the Review store, editor-decoration channel,
  and health/setup summary. Every additive Verify/Co-think query must consume the
  document GET endpoint's existing explicit SQLite snapshot connection; do not
  open independent read connections while assembling one response. SSE is only
  an invalidation nudge; the client always re-pulls authoritative state.
---

## Dashboard HTTP

The user-facing mutation/read surface is document-scoped:

- `POST /api/truth/doc/<document>/verify/runs`
- `POST /api/truth/doc/<document>/verify/recheck-target-affirmations`
- `GET /api/truth/doc/<document>/verify/runs/<run>`
- `GET /api/truth/doc/<document>/verify/configuration`
- `PATCH /api/truth/doc/<document>/verify/criteria/<criterion-key>`
- `POST /api/truth/doc/<document>/verify/criteria/drafts`
- `POST /api/truth/doc/<document>/cothink`
- `POST /api/truth/doc/<document>/cothink/items/<item>/actions`
- `POST /api/truth/doc/<document>/chat/action-snapshots`

Every mutation applies the existing Co-work gates: registered reachable store,
enabled document surface, active lifecycle, document policy, and non-read-only
dashboard. Starts require an exact capture and explicit provider/model.
Verify starts additionally require the user goal and protected intent currently
shown in the Verify dock. Recheck starts require the exact derived intent,
source run, still-pending proposals, original provider/model, original
goal/intent, and a fresh target capture. A durable recheck must resolve the
original target reference. A legacy intent without that reference requires a
separate non-executing human affirmation request. The server persists its
ActionSnapshot and returns a receipt whose character-range identity and
target-text hash must match the fresh Run capture.

There is intentionally no general agent-facing capability that starts Verify,
admits a checker, toggles a criterion, invokes Co-think, or decides a proposal.
Those are user/policy actions through the dashboard/domain boundary.

## R2 document projection

The existing document read adds:

- Verify capability/readiness;
- evaluation run summaries whose coordination status is derived from the
  portable lifecycle;
- coordinator-surfaced evaluation results;
- derived verification recheck intents;
- Co-think items and outcomes; and
- effective verification configuration.

Old clients may ignore the additive fields. `LiveReviewRailProvider` maps one
authoritative read into:

- the Review attention feed;
- Verify dock run history, setup counts, configuration, and execution plan;
- editor evidence decorations;
- Co-think cards/outcomes; and
- the existing proposal sitting.

Configuration and item mutations invalidate the provider. Truth SSE events are
also invalidation nudges; they never replace the authoritative repull.

The run-detail HTTP read adds the content-minimized coordination lifecycle,
request bindings, candidate-proof lineage, and consequence references used by
inspection. It never returns raw worker output or private candidate prose.

## Worker MCP

Job-scoped workers receive only:

- `cowork_verify_job_get(job_id)`
- `cowork_verify_job_submit(job_id, payload)`

Transport identity encodes the exact job and role. The caller argument is not
authority. The get response carries only the immutable context and
role-specific output schema. The submit path validates typed output and is the
only path that can create authorized consequences.

Targeted document Chat separately uses
`cowork_action_snapshot_get(store_id, document_id, action_snapshot_id,
message_id)` and a generation-bound consumption receipt. Available and
unavailable terminal fetches both produce receipts so an exact targeted turn
can receive a truthful response and acknowledgement. After a restart, a new
generation may reuse a previously committed stable reply only when both
receipts prove identical target/turn semantics.

## Events

Post-commit Truth events include:

- `truth.doc_verify_run_started`
- `truth.doc_verify_recheck_target_affirmed`
- `truth.doc_verify_job_completed`
- `truth.doc_verify_configuration_changed`
- `truth.doc_cothink_started`
- `truth.doc_cothink_item_added`
- `truth.doc_cothink_outcome_recorded`
- `truth.doc_cothink_item_status_changed`

Publication failure does not roll back Truth. The UI repulls after a received
event and remains correct after missed events or refresh because state is
durable.

## User-visible behavior

The compact editor-top target controls:

- show **Working on** and its target summary;
- make **Set by selection** the primary exact-character action;
- provide a secondary, two-step **Set by cursor** flow; and
- show the active target as view-only highlighting with start/end markers.

The editor-bottom docks:

- show sibling **Verify** and **Co-think** headers in one accordion;
- keep Verify target override, criteria, goal, protected intent, explicit
  provider/model, truthful execution disclosure, run action, progress, and
  history together;
- state separately that the deterministic checker runs in-process against the
  captured target while account-backed coordination receives the entire frozen
  document;
- derive cost language from the selected provider's enforcement class and
  never present a requested budget as a universal guarantee; and
- keep Co-think as a non-operational **Planned** shell in the current slice.

The Review rail:

- surfaces only coordinator-dispositioned results;
- uses evidence-first cards and exact passage navigation;
- sends correction proposals through the ordinary sitting; and
- labels historical Co-think cards and actions separately from evaluation
  results.

The Chat composer has one compact **About:** chip. Ordinary authored messages
inherit the current Working on target and capture its exact version at send
time; there is no separate sticky target switch.

After a Verify-linked proposal is applied, Review projects a durable
**Correction ready to recheck** card. Its action is a contextual handoff: it
opens the Verify dock and binds the original target source/reference,
provider/model, user goal, protected intent, source run, and proposal set.
Review never launches the recheck. Only **Run Verify** in the bound Verify dock
captures a fresh version and authorizes execution; refresh and event delivery
also remain non-executing.

When the original durable target reference still resolves, the dock rebinds it
automatically. A legacy scoped run without a resolvable reference requires the
person to choose and affirm an exact **Working on** passage before **Run
Verify** becomes the launch boundary. The dock sends the affirmation capture
through the separate non-executing endpoint and retains the server-issued
receipt. It compares a second capture at Run; a same-range text edit requires
renewed affirmation. The server reloads the attested ActionSnapshot, repeats
those checks, and binds the confirmation to the intent, lineage, original
goal/intent, and authorization receipt. Neither path may silently widen to the
whole document.

Clean conforming results remain quiet in the attention feed but inspectable in
run history. Results against an earlier structured head are labeled as an
earlier version instead of being presented as current.
