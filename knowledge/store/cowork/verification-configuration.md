---
name: Co-work Verify configuration
kind: system
description: Criterion-first, append-only configuration with separate authorship, activation authority, executor admission, and data-sharing facts.
summary: Effective Verify checks are derived from immutable criterion/check versions, bindings, and activation events. Human document overrides use compare-and-swap activation IDs. Personal checks use a user-authored criterion bound to a statically admitted system evaluator; legacy unadmitted drafts remain unavailable.
entry_points:
- work_buddy.cowork.verify_configuration
- work_buddy.cowork.verify.service
- dashboard-react/src/apps/cowork/verify/VerifyCheckControl.tsx
- dashboard-react/src/apps/cowork/targets/CoworkDocumentActionDock.tsx
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
  `create_user_verification_check` may bind declarative user semantics only to
  the exact system-owned instruction evaluator. `create_user_criterion_draft`
  remains a compatibility path which writes an unadmitted `model_judge_draft`;
  do not make those records runnable by weakening executor admission.
---

## Record model

Verify configuration is criterion-first:

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
same transaction that recomputes current state, so stale selection state fails
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

## Personal instruction-based checks

The Verify dock's **Add check** page accepts a name, evaluation instructions,
and optional exceptions. Saving creates:

- an immutable user-authored criterion;
- a binding to the immutable system-owned instruction evaluator; and
- an enabled, optional document activation authorized by the human.

The evaluator is admitted by exact mechanism, schema, executor reference, and
criterion kind. User text remains declarative data and cannot introduce code,
tools, a new executor, or a broader content boundary.

Personal checks are scoped to the exact document whose human activation
selected their binding. They do not appear in another document's menu or run
plan merely because their immutable definition exists in the same Truth
store. Disabled checks remain available in that document's menu, but a run
freezes and shares only the enabled criteria and bindings selected for that
run.

Evaluation instructions are limited to 8,000 characters. Oversized input is
rejected before seeding a built-in definition or writing any criterion, check,
binding, or activation record.

Legacy `model_judge_draft` records with `unadmitted:` executor references remain
readable, disabled, and unavailable. They are kept in a separate stable-key
namespace and are never silently upgraded. General executor/plugin admission
remains outside the user-facing surface.

## User behavior

The full-workspace Verify dock presents configuration as the person's actual
task:

- a **Checks** menu with selected state;
- built-in versus personal origin where useful;
- an **Add check** replacement page; and
- **Run Verify**.

Review contains results and decisions, not configuration. Internal criterion
versions, executor admission, provider/model authorization, call topology,
cost, egress, and failure telemetry remain inspectable domain facts rather than
primary run-flow prose.

The deterministic terminology check runs in-process against the captured
target. Personal instruction-based checks use narrow account-backed specialist
evaluations of that same target. A run admits at most five such selected
model-backed checks. Whole-context coordination is separate and receives the
permitted complete frozen document plus the active run-only configuration.
Provider/model fallback stays off and failure remains fail-closed.
