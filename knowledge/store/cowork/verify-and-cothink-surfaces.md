---
name: Co-work Verify and Co-think surfaces
kind: system
description: HTTP, MCP, event, Review, Chat, and editor projections for Co-work Verify and Co-think.
summary: One Co-work action bar launches sibling Verify and Co-think actions against a shared exact document target. Dashboard HTTP carries editable Verify intent; R2 projects authoritative results, portable coordination, Co-think, and recheck state; job-scoped MCP exposes only exact worker get/submit operations.
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
shown in the action bar. Recheck starts require the exact derived intent,
source run, still-pending proposals, original provider/model, original
goal/intent, and a fresh capture of the original target reference.

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

- the Review attention feed and run history;
- Verify setup counts and configuration;
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

The document action bar:

- shows **Working on** and its target summary;
- exposes whole document, current section, selection, and custom-range actions;
- uses one **Action target** chooser for both sibling subsystems;
- shows active/unavailable Verify counts and the selected provider/model;
- lets the person edit **Goal and protected intent** before **Run Verify**;
- explains Verify’s whole-context coordination and data-sharing/cost boundary;
  and
- gives Co-think a separate non-evidential explanation and **Invite
  perspective** button.

The Review rail:

- keeps Verify setup collapsible at the top;
- shows run status/history and an inspectable frozen plan;
- surfaces only coordinator-dispositioned results;
- uses evidence-first cards and exact passage navigation;
- sends correction proposals through the ordinary sitting; and
- labels Co-think cards and actions separately from evaluation results.

After a Verify-linked proposal is applied, Review projects a durable
**Correction ready to recheck** card. **Recheck now** is an explicit user
action; refresh and event delivery never start the model. The button preserves
the original target source/reference, provider/model, user goal, protected
intent, source run, and proposal bindings. Older scoped runs without a
resolvable target reference instead request an explicit target choice.

Clean conforming results remain quiet in the attention feed but inspectable in
run history. Results against an earlier structured head are labeled as an
earlier version instead of being presented as current.
