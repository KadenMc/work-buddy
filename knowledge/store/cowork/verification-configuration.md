---
name: Co-work Verify configuration
kind: system
description: Criterion-first, append-only configuration with separate authorship, activation authority, executor admission, and data-sharing facts.
summary: Effective Verify setup is derived from immutable criterion/check versions, bindings, and activation events. Human document overrides use compare-and-swap activation IDs. User-authored criteria are stored as disabled, unavailable drafts until a separately reviewed executor is admitted.
entry_points:
- work_buddy.cowork.verify_configuration
- work_buddy.cowork.verify.service
- dashboard-react/src/apps/cowork/rail/VerifySetupCard.tsx
tags:
- cowork
- verify
- criteria
- checks
- configuration
- executor-admission
- provenance
aliases:
- Verify setup
- verification criteria
- criterion activation
- user-authored checks
- system checks
parents:
- cowork/verify-and-cothink
dev_notes: |-
  `_configuration_projection` is the effective-state reducer. Preserve the four
  independent facts: definition origin/author, activation authorizer, executor
  availability, and data-sharing classification. Never infer one from another.
  `create_user_criterion_draft` intentionally writes an unadmitted
  `model_judge_draft`; do not make it runnable by weakening
  `_check_availability`.
---

## Record model

Verify setup is criterion-first:

- `CriterionDefinitionVersion` states what the work is expected to satisfy.
- `CheckDefinitionVersion` states one evaluation mechanism, its schemas,
  limitations, provenance, and executor reference.
- `CriterionCheckBinding` configures one check for one criterion.
- `CriterionActivation` records where the binding is enabled or required and
  who authorized that applicability.
- `EvaluationPlanSnapshot` freezes the effective records used by one run.

Definitions are versioned and activations are append-only. Changing a toggle
does not rewrite history or alter a running evaluation.

## Four separate trust questions

The projection answers these independently:

1. Who authored or published the criterion/check?
2. Who authorized this criterion to apply to this document?
3. Is the executable mechanism admitted and available?
4. What content leaves the machine when the method and coordinator run?

A user is authoritative about their preference, but that does not establish
that a generated detector is accurate or safe to execute. A built-in executor
can be admitted while its criterion remains optional.

## Effective activation

The reducer considers applicable policy activations and document-specific
human overrides. A required policy activation stays locked. A human can enable
or disable an optional criterion for one document.

The HTTP toggle carries `expected_activation_id`. The mutation occurs in the
same transaction that recomputes current state, so a stale setup card fails
with a reload instruction instead of overwriting a newer decision.

Enabling fails closed when the selected check is unavailable. Required but
unavailable checks project as `blocked_required_check`; they are not silently
skipped or converted into a pass.

## Built-in exact-term criterion

The shipped executable criterion checks configured non-preferred terms against
their preferred replacements with exact string matching. Its method is:

- deterministic;
- local-only for the check itself;
- admitted only for the exact built-in executor reference; and
- limited to complete exact-string coverage of the frozen target.

An initial read projects the built-in default without mutating the store.
The first configuration mutation or evaluation seeds the immutable default
records. The whole-context coordinator and optional reviser are separate
account-backed model calls; the fact that the check is local does not make
those calls local.

## User-authored criterion drafts

The setup card can save:

- a criterion name and positive statement;
- proposed evaluation instructions; and
- known limitations.

Saving creates immutable user-authored criterion, proposed-check, binding, and
disabled activation records. The check is explicitly labeled
`model_judge_draft` with an `unadmitted:` executor reference. It:

- does not execute a model;
- does not share document content;
- does not become enabled; and
- cannot be toggled on.

There is no user-facing executor-admission capability or UI. Making a draft
runnable requires a separately reviewed and implemented executor definition,
admission policy, evaluation evidence, data-sharing disclosure, and an
authorized activation. “Save unavailable draft” is therefore an honest capture
of the desired criterion, not a promise that Work Buddy automatically invented
a trustworthy verifier.

## User behavior

The Review rail’s collapsible **Verify setup** card shows:

- active and unavailable counts;
- built-in versus user-authored origin;
- optional, required, and locked state;
- activation authority;
- check method/version and limitations;
- local/external data-sharing status; and
- the account-backed coordination boundary, maximum worker calls, and per-worker
  cost ceiling.

The document action bar shows the current active/unavailable summary and the
explicit provider/model that a new run will use.
