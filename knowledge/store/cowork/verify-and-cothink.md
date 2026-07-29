---
name: Co-work Verify and Co-think
kind: system
description: Exact-version document evaluation and deliberately non-evidential support for human deliberation inside the Co-work document surface.
summary: Co-work Verify and Co-think are sibling editor docks over one shared document target. Verify evaluates explicit criteria through a coordinator, optional reviser, deterministic candidate proof, and post-revision coordinator before ordinary human review. Co-think currently exposes only a planned sibling shell; its fuller deliberation workflow is deferred.
entry_points:
- work_buddy.cowork.verify
- work_buddy.cowork.verify_orchestration
- work_buddy.cowork.verify_coordination
- work_buddy.cowork.verify_candidate_evaluation
- work_buddy.cowork.verify_api
- dashboard-react/src/apps/cowork/rail/VerificationAttentionFeed.tsx
- dashboard-react/src/apps/cowork/rail/VerifySetupCard.tsx
tags:
- cowork
- verify
- co-think
- evaluation
- document-target
- human-review
- mixed-initiative
aliases:
- Co-work Verify
- Co-think
- document evaluation
- evaluation and revision
- alternative perspective
parents:
- cowork
dev_notes: |-
  The Truth schema carries immutable definitions, action snapshots, plans, runs,
  executions, results, dispositions, relations, model-call authorizations,
  Co-think items, and Co-think status events. Operational worker state lives in
  `verify_runtime.py` and is never the authority for document or review facts.

  Keep the two subsystems semantically separate in every projection. An
  `EvaluationResult` may report conformity or nonconformity against a criterion.
  A `CothinkItem` is an advisory alternative perspective with open/parked/dismissed
  lifecycle state. Do not route a Co-think item through result severity, pass/fail,
  or the proposal sitting.
---

## Product boundary

Co-work Verify and Co-think are subsystems of the existing Co-work document
surface. They reuse its registered document identity, Yjs-backed editor,
canonical Markdown projection, Review rail, Chat conversation, account-backed
execution selection, immutable proposals, and two-phase human sitting.

Co-work Verify answers:

> What did this exact version of the work satisfy or fail to satisfy under these
> explicit criteria, methods, and evidence limits?

Co-think answers:

> What useful alternative perspective might help the person deliberate about
> this exact work?

Sharing the same workspace and action snapshot does not make their outputs the
same type. Verify produces typed evaluation records. Co-think produces an
advisory item or an honest no-useful-item outcome.

## Sibling dock experience

The editor top keeps one compact **Working on** control. The editor bottom has
an accordion with sibling **Verify** and **Co-think** docks; only one is open at
a time and both may be collapsed. Expanding a dock reduces only editor space.
It does not overlay or displace Review or Chat.

The Verify dock owns the current target or one-run override, criteria and check
configuration, user goal, protected intent, explicit account-backed
provider/model, execution and data-sharing disclosure, run action, progress,
and history. Review receives only durable results, proposals, rechecks, and
human decisions.

The Co-think dock is currently a non-operational **Planned** shell so the
sibling interaction and responsive footprint can be tested without treating
the earlier one-shot perspective worker as a settled product. Historical
Co-think items may still appear in Review.

Ordinary Chat messages use the shared Working on target and show it as one
compact **About:** chip in the composer. The target is frozen for that message;
changing Working on or editing the document afterward does not rewrite it.

## Current end-to-end behavior

1. The user chooses or confirms Working on and, in the Verify dock, may choose
   a one-run override and edit the exact user goal and protected intent.
2. The editor freezes one exact document/action snapshot and the server
   validates its structured head, selector, projection, target text, and hashes.
3. Verify resolves and freezes the effective criterion/check configuration,
   then the admitted deterministic terminology check evaluates the frozen
   target.
4. A job-scoped coordinator receives the complete permitted frozen projection,
   target, user goal, protected intent, effective configuration and policy,
   normalized results, relevant prior dispositions, and prior human review
   outcomes.
5. The coordinator may retain a result, surface it, escalate it, or request a
   separate revision candidate. Raw check output cannot publish to Review.
6. A requested reviser sees only the selected findings and creates a private
   candidate. The server then re-evaluates the affected region, including
   boundary-spanning configured terms, and binds a hashed deterministic proof
   to the post-revision coordinator.
7. The post-revision coordinator may route a correction only when that proof
   passes. The domain service then creates an ordinary immutable proposal.
8. A human confirms, amends, rejects, redirects, endorses, or defers through
   the existing review sitting. Only the sitting may apply the exact payload.
9. An applied Verify-linked correction creates a persistent recheck card.
   Review hands that context into the Verify dock; it does not launch a run.
   The dock preserves the original provider/model, goal, protected intent,
   source run, and still-pending proposal set. It automatically rebinds a
   resolvable durable target, while a legacy unresolved target requires a newly
   affirmed exact **Working on** passage. That affirmation is persisted by a
   separate non-executing request before Run can use its server receipt.
10. Only **Run Verify** in the bound dock captures the fresh version and
    authorizes recheck execution. Silent widening to the whole document remains
    forbidden.

The durable Co-think records and worker contracts from the earlier
alternative-perspective slice remain readable for compatibility. The current
document surface does not expose a new invocation until the planned
deliberation contracts are reviewed. A historical delivered item retains
**Discuss**, **Keep for later**, and **Dismiss**.

## Shared foundations, different authority

Both subsystems share:

- the registered document and exact structured head;
- document-target capture and immutable action snapshots;
- explicit provider/model selection and content-bound authorization;
- job-scoped least-authority worker sessions;
- durable history and event-driven Review refresh; and
- passage navigation back into the editor.

They do not share:

- evidential status;
- result vocabulary;
- disposition rules;
- proposal authority; or
- user response semantics.

The coordinator may eventually suggest that another perspective could be
useful, but Co-think invocation must remain an explicit user action. A
Co-think item cannot activate a criterion, establish a nonconformity, or
create/apply a correction.

Coordination also has a content-minimized portable projection in the Truth
store. Export/import preserves exact bindings, provider/model, user goal,
protected intent, frozen configuration and policy references, prior decision
references, lifecycle outcomes, consequence references, and deterministic
candidate proof. It deliberately omits raw frozen document bytes, raw worker
output, and private candidate prose. The local runtime database is operational
state, not the portable history.

## Terminology

**Co-work Verify** is the local product name for the evaluation-and-revision
subsystem. It is not a claim that every evaluation method is verification in
the standards sense. Use the narrower words **verification**, **validation**,
**review**, **nonconformity**, and **correction** only when their conditions are
actually met.

**Co-think** is the local subsystem name for support for human deliberation.
Name the specific interaction—alternative perspective, reflective prompt,
Socratic questioning, clarification question, or cognitive forcing
function—instead of treating “provocation” or “cognitive friction” as a single
result type.

**Agentic-output-verification (AOV)** names the old framework/codebase and
its historical records only. It is not an abbreviation for Co-work Verify.

**Artifact assurance** is not adopted as a standard field term or product
name. The terminology audit treats it only as a partially attested compound
whose bundled meaning is not conventional. Describe the actual composition
instead: evaluation against explicit criteria, applicable validation,
feedback-based revision, exact-version human review, and separate
authorization to apply.

The research search aliases remain useful for literature discovery but are not
adopted as canonical product stages:

- drafter–auditor–refiner
- draft–score–refine
- generator–critic
- drafter–critic
- verify–then–refine
- evaluator–optimizer
- critique-and-revise
- self-correction
- self-verification
- self-refinement

Describe the implemented composition in established terms: **evaluation
against explicit criteria, feedback-based revision, re-evaluation, and exact
human review before application**.

## Current capability boundary

The executable evaluation set contains one admitted deterministic exact-term
check. User-authored criteria can be saved as disabled, unavailable drafts, but
there is no user-facing executor-admission mechanism. The Co-think dock is
present but non-operational. Model-based specialists, source-grounded checks,
multi-turn Socratic inquiry, ambient prompts, named return conditions, and
blocking publication policy remain extension points.

Related units:

- `cowork/verify-authority-and-orchestration`
- `cowork/verification-configuration`
- `cowork/document-targets-and-action-snapshots`
- `cowork/verify-dispatch-and-rechecks`
- `cowork/cothink`
- `cowork/verify-and-cothink-surfaces`
