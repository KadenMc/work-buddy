---
name: Source-backed Journal capture
kind: system
description: Durable Quick Capture path that commits exact input to Sources before Journal routing or optional processing.
summary: Journal Quick Capture is a source-first write. Exact input and a reference-only command/outbox commit atomically; native Journal creation, routing, and optional processing reconcile independently, so a failed effect cannot lose or masquerade as a failed save.
entry_points:
- work_buddy.journal_capture.api
- work_buddy.journal_capture.service
- work_buddy.journal_capture.smart
- work_buddy.journal_capture.smart_worker
- work_buddy.journal_capture.dispatch
- work_buddy.journal_capture.store
- work_buddy.journal_capture.domain
- work_buddy.journal_capture.native_source
- dashboard-react/src/apps/journal/providers/HttpJournalProvider.ts
tags:
- journal
- capture
- sources
- quick-capture
- running-notes
- recovery
aliases:
- real Journal capture
- durable Quick Capture
parents:
- journal
requires:
- architecture/source-foundation
dev_notes: |-
  Preserve the established payload: `target_id = auto | log | running_notes`, `mode = dumb | smart`, exact text, input mode, optional stated time, and client mutation ID. There is no `both` target. `auto + dumb` is invalid.

  A task proposal is a typed optional follow-up, never a CaptureTarget or TaskStore record. `follow_up_action=task_proposal` with `running_notes + dumb` is the explicit model-free Save and propose task action. Smart may return one task-proposal follow-up alongside Journal placement. Journal's existing effects table is the delivery outbox; Threads is the sole proposal authority.

  Smart's displayed provider/model/input-limit disclosure is hash-bound into the human gesture and durable inference effect. A changed or missing boundary fails processing after the source save; it cannot silently disclose to a different provider. A fresh explicit retry may bind the newly reviewed boundary.

  A persistence acknowledgement means Sources and the reference-only command/outbox committed. It does not mean optional smart processing succeeded. The UI clears its draft only after persistence and presents delayed/failed processing separately.

  Before the database authority seal, stable hidden entry markers keep the frozen Markdown compatibility writer occurrence-safe. Database-only capture bypasses that adapter and creates independently addressable native items.
---

# Source-backed Journal capture

The production Journal provider sends Quick Capture to
`POST /api/journal/captures`. The protected route derives the actor from the
enrolled local session and consumes a one-use gesture bound to the exact
semantic request. It accepts no trusted actor fields from the caller.

Sources atomically records the exact representation, ingress submission,
versioned Journal command, and source outbox entry. Journal then creates its
capture/effect records and, under database authority, an independently
addressable native item in the frozen logical-day composition. The legacy
`JournalContentAdapter` is reachable only in the pre-seal compatibility epoch.
A recovery sweep drains missed outbox work on startup.
If a long-running operation outlives its Sources lease, Journal re-leases only
to acknowledge the already durable result, without repeating inference or
materialization. A live competing lease or expired authorization is not bypassed;
the exact-text save acknowledgement remains independent of delayed outbox receipt.

Direct Log and Running Note captures need no model. `auto` routing and optional
smart annotation are separate effects. The production Smart processor is
disabled by default. The canonical `wb.journal.smart-processing` setting bootstraps
from `journal.smart_processing.enabled` and then owns the explicit opt-in.

`wb.journal.smart-execution` owns which of two execution paths an enabled Smart
capture uses. Both satisfy the same Journal contract, and the choice is the
user's because they trade differently. `subscription_agent`, the default,
launches a detached least-authority worker on the account the user already
signs in to. It follows `wb.dashboard.chat-execution-default`, adds no separate
model charge, and settles a capture more slowly. `api_model` calls one API
endpoint in process for the shortest wait and bills that API account for each
capture. It keeps the concrete model bound to `journal.smart_processing.tier`,
because the chat default names an agent runtime rather than an API endpoint.
Each path preflights its own concrete provider and model before any capture
text moves, projects that pair through typed availability, and states its own
disclosure line. Neither path substitutes for the other when its provider is
unavailable.

Whichever path is active, Smart displays the provider/model boundary through
the Journal capability description and records a content-free Agent Execution
disclosure manifest before the exact retained capture reaches the model. The
model boundary is capped at 32 KiB and never silently truncates;
larger captures remain saved while optional processing fails visibly. Neither
path has web access, rewrites the saved text, or reuses a cached model
response, and both bind one validated structured result back to the Journal
capture. The in-process path carries no tools at all. The detached worker holds
exactly two lease-bound capabilities, `journal_smart_processing_context` and
`journal_smart_processing_complete`, which its session ACL pins to one request
and refuses for every other session. Its brief carries only an opaque request
ID and a lease secret, never capture text, and one spawn is bounded far below
the general agent allowance because a capture is a single bounded
classification. Context resolves the retained Source under a Journal service
principal and records the input manifest before returning the saved text.
Complete validates the same structured shape the in-process path validates.
A settled result is never automatically sent again. Typed availability
distinguishes `disabled_by_policy`, `provider_unavailable`, and `ready`, with a
visible Settings link or setup retry. Idle explanations use shared hover/focus
help. While Smart is on and ready, a compact provider/model/input-limit line
remains visible before capture.

A Smart result retains a Log or Running Notes placement and may attach one
`TaskProposalFollowUp`. Task-like Auto captures become open Running Notes.
The model result, annotation, and proposal delivery command commit atomically
before any cross-store delivery. The outbox calls Threads `ActionProposalService`
with stable `journal-task-proposal:<captureId>:v1` ingress identity, hash-bound to
the task parameters and Source-backed origin. An uncertain retry gets the same
Thread. Journal records that Thread reference and terminal receipt reconciliation
checkpoint, not another proposal lifecycle.
No capture path invokes TaskStore create.

Recent Capture and Running Notes expose provider-validated generic follow-up
links: `/app/tasks?proposal=th-…` until realization, then `/app/tasks?task=t-…`.
Model output cannot supply navigation. Creating or rejecting a proposal does not
resolve its note. Read-only projections can immediately display a realized task
link, but canonical `routed_to_task` advances only in explicit bounded proposal
maintenance after a matching Threads realization with TaskStore receipt and
revision. Rejection checkpoints delivery as terminal while leaving the note open.
Neither view GET nor follow-up link projection creates or resolves a proposal.
Source redaction cancels and scrubs unsent Journal proposal parameters before
compatibility-file redaction; later maintenance and retries cannot materialize
that removed input into a new Thread. Already delivered Thread proposals are
independently retained derivatives, not covered by Journal's exact-copy scrub.
The delivery boundary rechecks the live owned effect before ingress, and a late
success or failure cannot reopen a source-removal pause. An already-started
cross-store commit is not revoked by Journal's cancellation acknowledgement.
Delivery failure remains a successful exact-text save with a visible retry.
**Save and propose task** works while Smart is disabled, without model inference.
Both save controls share an immediate single-flight lock and visible saving
feedback until the owning App returns. The existing device draft retains the
pending mutation ID and exact-request hash for unchanged uncertain retries;
accepted saves clear only the unchanged draft revision, preserving later edits.
The pending envelope separately retains the reviewed Smart-disclosure hash.
Automatic destination or disclosure changes preserve that identity and pause a
mismatched retry; only an explicit draft edit or destination/mode choice starts a
new capture. A selected Smart switch stays available to turn off when a provider
goes away. Submit and follow-up retry intents carry the disclosure hash frozen
at click time, before draft flushing or identity awaits. The HTTP provider
validates and forwards that hash, never recomputing it from a newer view snapshot.

New Running Notes have stable entry IDs, revisions, Source dependencies, and
tombstones. A role whose immutable interaction contract declares a document
can bind to Co-work from creation. Contextual and full-workspace editors open
the same session; navigation does not create a document or change Truth state.
Database-only Journal items have no Markdown projection.

A capture may state when it happened rather than when it was typed. The stated
instant becomes the record's occurrence time, so a Log entry written after the
fact takes its real position in the day's ordering instead of the moment of
capture. Leaving it unstated files the capture at its submission time.

A stated instant must be unambiguous and must fall inside the window of the day
the capture names, which is not necessarily today. Both facts are enforced where
the day is known rather than only in the browser, so a request that reaches the
server directly is bounded the same way.

Legacy Journal Markdown is accepted only through the private staged history
import. Every input byte receives a deterministic disposition and exact Source
retention before the inactive cohort can be verified and sealed. The Journal
authority seal publishes the cohort and permanently fences normal Markdown
reads and writes; the original tree remains a detached read-only archive. The
older per-passage Co-work migration operator remains recovery-only and is not
the steady-state Journal model.
