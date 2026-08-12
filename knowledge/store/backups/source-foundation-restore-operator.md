---
name: Source Foundation Restore Operator
kind: capability
description: Inspect or explicitly reconcile the fail-closed Source Foundation fence left by a machine restore.
capability_name: source_foundation_restore_operator
category: backups
op: op.wb.source_foundation_restore_operator
schema_version: wb-capability/v1
parameters:
  action:
    type: string
    description: status or reconcile.
    required: true
  snapshot_id:
    type: string
    description: Exact active snapshot ID; required for reconcile.
    required: false
  identity_enrollment_path:
    type: string
    description: Optional exact sanitized enrollment manifest path; defaults to the member retained by data_restore.
    required: false
  disclosure_outcomes:
    type: object
    description: Exact possibly_sent disclosure entry IDs mapped to proven sent or not_sent outcomes. The operator never retries transport.
    required: false
  reconstitute_missing_identity:
    type: bool
    description: Rebuild a missing local identity database from the snapshot's sanitized enrollment only; no credential authority is restored.
    required: false
  sources_archive_path:
    type: string
    description: Exact authorized Sources archive used only when the machine Sources authority is missing.
    required: false
  truth_recovery_targets:
    type: object
    description: Permanent Truth store IDs mapped to explicit existing Folder roots for portable ledger plus causality recovery.
    required: false
  quarantine_truth_store_ids:
    type: list[str]
    description: Exact unavailable or intentionally deferred Truth inventory identities to quarantine without inferring recovery.
    required: false
  quarantine_missing_cohorts:
    type: list[str]
    description: Explicitly abandon and reconstitute only missing journal_capture or cowork_conversations authority state.
    required: false
  defer_source_effect_ids:
    type: list[str]
    description: Exact imported_inert paused Sources effects to defer until a separately approved post-restore recovery.
    required: false
mutates_state: true
consent_operations:
- source_foundation.restore_reconcile
retry_policy: manual
auto_retry: false
tags:
- backups
- restore
- sources
- truth
- provenance
- recovery
parents:
- architecture/backups
- architecture/source-foundation
---

`status` is content-free and read-only. `reconcile` requires a fresh, exact,
high-risk approval. It compares the stable IDs in the sanitized identity
enrollment with the live enrollment. With explicit consent it can build the
current identity schema from those four stable IDs when the authority database
is absent, but it never imports browser sessions, CSRF tokens, bootstrap
tokens, or human-authority gestures.

The approval fingerprint binds the frozen marker inventory, enrollment and
Sources archive digests, resolved paths, exact Truth permanent identities and
targets, quarantine identities, deferred effect IDs, and disclosure outcomes.
A changed marker or changed archive is rejected after approval.

Fresh or partial restores converge through explicit recovery, never guesswork:
missing Sources can be rebuilt from a same-authority authorized archive;
nonterminal commands/effects remain paused and require a later fresh
`sources.maintenance` approval. Portable Truth JSONL and its identity-bound
document-causality companion are imported together into an explicit empty
Folder. Unavailable Truth inventory or non-portable missing Journal/
conversation state can instead be named for durable quarantine. Existing but
incoherent authorities are never overwritten by those quarantine paths.

Agent Execution and Sources are compared reservation by reservation. A
`possibly_sent` entry remains blocked until the caller supplies a proven
outcome; the operator records that outcome and Sources acknowledgement without
replaying source bytes or invoking a model/provider transport. Known outcomes
may resume accounting-only acknowledgement.

The operator also re-hashes every retained Sources representation/blob and
checks the content-free Co-work conversation dependency ledger against every
retained user/agent message and its exact conversation/store/document owner.
`review_required` derivatives and missing dependency rows remain blocking;
they are never inferred settled from absence or current text. Journal and
task-note authority epochs, projection state, and document heads are likewise
cross-checked against each registered store's portable causality cohort.

The marker is not cleared when any cohort is missing, malformed, mismatched,
or has unsettled recovery work. A successful clear atomically moves the marker
to an audit receipt instead of deleting it.
