---
name: Co-work Verify authority and orchestration
kind: system
description: Coordinator-first routing, role separation, and the human-only authority boundary for Co-work Verify.
summary: Check output never reaches Review directly. Selected in-process and model-backed checks finish before their normalized results go to a whole-context coordinator; requested revisions remain private and require an admitted affected-region evaluator before ordinary human review.
entry_points:
- work_buddy.cowork.verify_orchestration
- work_buddy.cowork.verify_coordination
- work_buddy.cowork.verify_candidate_evaluation
- work_buddy.cowork.verify.service
- work_buddy.cowork.verify_jobs
- work_buddy.cowork.ops
tags:
- cowork
- verify
- coordinator
- authority
- human-in-the-loop
- proposal
- least-authority
aliases:
- coordinator-first Verify
- delegator boundary
- no raw verifier output
- whole-context coordinator
parents:
- cowork/verify-and-cothink
dev_notes: |-
  `_build_job_context` is the authority-bearing context assembly point.
  `_validate_coordinator_output` enforces one decision per assigned normalized
  result, requires conforming results to be retained quietly, and refuses
  `route_to_correction` without a passing deterministic affected-region proof.
  `_process_coordinator` is the only path that records routing dispositions and
  asks the proposal service to create a correction. Keep worker stdout and final
  responses non-authoritative.
---

## Why the coordinator is mandatory

A narrow check can see one defect while missing the document’s purpose,
protected intent, surrounding argument, other results, or a conflicting
revision. Co-work Verify therefore treats specialist output as input to a
decision, not as a Review-rail contribution.

The coordinator receives the complete permitted frozen Markdown projection
under its exact model-call authorization, plus:

- the exact document and target identities and hashes;
- the user goal and protected intent;
- frozen criteria, check definitions, limitations, and execution diagnostics;
- every normalized result assigned to the run;
- relevant prior routing dispositions;
- relevant prior human proposal/sitting outcomes;
- the frozen run-only configuration containing enabled criteria and bindings;
- the frozen configuration and policy hashes;
- the allowed change and egress boundaries; and
- a private revision candidate plus its deterministic affected-region proof
  when one was requested.

If an authorized coordinator is unavailable, the run remains inspectable as
unavailable. Undispositioned results do not bypass the coordinator into Review.

## Role boundaries

### Check evaluator

Evaluates the frozen target under one admitted method and emits structured
results. It cannot route, configure, propose, decide, or apply.

The deterministic terminology evaluator runs in the domain process. A personal
instruction-based check runs in a narrow specialist worker which receives only
the exact target text and hash, one criterion/binding, and its typed output
schema. It does not receive document title, full-document prose, selector
prefix/suffix context, or text outside the captured target. The server validates
the result kind, severity, coverage, limitations, and exact evidence, then
derives canonical prefix/suffix context from the frozen target rather than
trusting model-supplied context.

All selected evaluators must complete before the coordinator starts. A run
admits at most five selected account-backed specialist checks. A missing,
invalid, or unavailable evaluator fails the run closed; raw specialist prose
cannot surface.

### Coordinator

Makes one typed decision for every assigned normalized result. The current
decision vocabulary supports:

- retain without interrupting;
- surface an evidence-bearing result;
- escalate;
- request a private revision; and
- after revision, route an accepted candidate to a correction proposal.

A conforming result must be retained. A revision can be requested only for a
finding whose check family has a separately admitted candidate evaluator.
Model-backed personal checks currently support retain, surface, and escalation,
not automatic private revision. Routing never upgrades the evidential strength
of the underlying result.

### Reviser

Receives only the finding results selected by the first coordinator and drafts
typed replacement candidates. A candidate is not an edit, proposal, decision,
or correction merely because it was generated.

### Post-revision coordinator

Receives the whole permitted frozen document, the original normalized finding,
the candidate, and a server-generated evaluation of the changed region. For
the admitted terminology check, that deterministic evaluation includes enough
surrounding text to detect configured terms spanning the replacement boundary.

The server recomputes the proof from the frozen projection, exact evidence,
candidate, and effective configuration; a portable proof must match that
authorized context. `route_to_correction` is rejected unless the check family
admits that evaluator and the proof passes. Only then may the post-revision
coordinator decide that the candidate fits globally well enough to be routed
to the proposal service.

### Human reviewer and sitting

The human remains the substantive decision authority. A coordinator-routed
candidate becomes the same ordinary immutable `ProposalRecord` used by the
rest of Co-work. The existing two-phase sitting binds the exact proposal hash,
current structured head, and application receipt. Verify does not write to the
Y.Doc or Markdown file.

## Worker authority

Every model role has:

- an explicit account-backed provider and model;
- a deterministic job and session identity;
- one immutable job context hash;
- an exact content boundary, egress class, cost ceiling, retry limit, and
  expiry in a `ModelCallAuthorizationReceipt`; and
- a least-authority MCP allowlist containing only job get/submit operations.

`cowork_verify_job_get` derives authority from the gateway-injected worker
session, not from the caller-supplied job ID. `cowork_verify_job_submit` is the
only authoritative delivery path. Identical submission is idempotent; a
different second payload is rejected.

## Portable coordination history

Operational jobs and raw typed worker payloads stay in the local runtime
database. The Truth store separately records a content-minimized,
append-only coordination projection that survives export/import without that
database:

- exact document/action/plan/run, parent-job, authorization, and context hashes;
- provider/model selection;
- user goal, protected intent, complete effective configuration and policy
  hashes;
- active criterion, prior disposition, and prior human review outcome IDs;
- recheck and coordinator-stage lineage;
- deterministic candidate-evaluation proofs containing hashes, status,
  coverage, and relative matches rather than candidate prose; and
- sanitized lifecycle states, output hashes, outcome kinds, error codes, and
  consequence record IDs.

It does not export the frozen document bytes, raw worker output, private
revision candidate text, or model rationale. Import validates exact fields,
hashes, transitions, and referential order before rebuilding the inspectable
history. For specialist chains it also proves that every sequence position
corresponds to an admitted account-backed assignment, parent links are
contiguous, the final specialist hands off to the initial coordinator, and
completed consequence IDs equal the full assigned execution/result set.
Authorization receipts must preserve the exact provider/model, context,
captured-target boundary, human authorization, fixed per-worker ceiling, and
zero model-call retry policy.

## Review projection

`surfaced_results` joins each result to its latest coordinator disposition.
Raw or undispositioned results remain absent from the normal Review feed. Run
inspection may show the typed check/result/disposition ledger, but never treats
private worker prose or stdout as a domain result.
