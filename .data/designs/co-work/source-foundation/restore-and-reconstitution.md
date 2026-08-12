# Source Foundation restore and reconstitution

**Implementation status:** implemented in the Source Foundation branch.

## Why restore ends in read-only mode

The machine backup cannot pretend that every Source Foundation authority was
captured at one instant. Agent Execution and task-note migration are hot-backed
machine databases; retained Sources, Journal capture state, and local identity
are preserved from the live installation because their sensitive content is
not admitted to the unencrypted archive; scoped Truth stores remain beside the
Folders they govern and use portable recovery exports.

After the machine database swap, `data_restore` therefore writes
`db/source_foundation_restore_pending.json`. Its existence—not a setting—is the
central authority fence. While present, normal Source/Truth/causality/Agent
Execution/task-note/Journal/Hindsight writes and dispatches are rejected before
their irreversible boundary. Exact reads remain available when the existing
schema and identity can be validated. Missing, malformed, or older state does
not get silently initialized or migrated under the fence.

The marker is written into the validated staging directory before its atomic
rename into place. A completed database swap therefore cannot expose restored
authorities during a crash window without also exposing the read-only fence.
The Co-work conversation dependency database is hot-backed for disaster
recovery; during a local restore its live copy is preferred because retained
conversation history lives outside the swapped machine-database directory.
Reconciliation verifies those rows against that retained conversation state.

## Portable scoped Truth recovery

Each successfully covered Truth store contributes three related members:

1. `store.yaml`, preserving permanent store identity and policy;
2. `claims.jsonl`, preserving the deterministic Truth ledger and blobs;
3. `document-causality.json`, preserving bindings, prepared/committed document
   changes, projection intents, projection receipts, and projection cursors.

The causality companion is an identity-bound envelope over
`DocumentCausalityStore.export_bundle()`. The backup manifest records both the
archive-member digest and the inner payload digest. Import accepts the
companion only beside the matching Truth ledger, validates the permanent
`store_id`, validates every referenced document against the imported ledger,
requires a clean causality target, checks row counts and SQLite foreign keys,
and publishes the staged sidecar atomically. A missing causality database makes
that scoped store's backup coverage explicitly `error`; it is never represented
as an empty history.

## Reconciliation operator

`source_foundation_restore_operator` has a read-only `status` action and a
fresh, per-invocation high-consent `reconcile` action. Reconciliation checks:

- the restore marker itself;
- stable local identity enrollment;
- Agent Execution entries against their exact Sources reservations, digests,
  lengths, representation IDs, and redaction epochs;
- every live inline/blob Sources representation against its digest, length,
  registered path, and reference count;
- Co-work conversation-source dependency rows against retained message
  identity/content, with `review_required` remaining blocking;
- unsettled Sources effects;
- Journal and task-note migration persistence;
- Truth registry/store identity and SQLite integrity;
- each Truth store's document-causality identity, referenced documents, and
  incomplete changes;
- unsettled Truth-to-Hindsight delivery and cleanup state.

The reverse inventory is also fail-closed: every retained user/agent
conversation message must have an exact dependency row with matching
conversation, store, document, and role ownership. Journal/task authority
epochs and projection heads are cross-checked against their registered Truth
document-causality authority rather than accepted from a migration mirror
alone.

The sanitized `local_identity_enrollment.json` is only an explicit trust input.
The operator compares its four stable enrollment fields with the live identity
database and records a digest-bound trust receipt. It never imports or mints a
browser session, bootstrap token, CSRF token, or human-authority gesture.

All Source Foundation SQLite authorities open with SQLite `mode=ro` plus
`query_only` while fenced. Truth's filesystem-only Y.Doc append, compaction,
snapshot, recovery-marker, and blob-cleanup boundaries are fenced too, so
read-only means more than merely rejecting `BEGIN IMMEDIATE`.

An Agent Execution entry in `possibly_sent` is a hard blocker. The operator may
record only an explicitly supplied proven `sent` or `not_sent` outcome. It does
not resolve source bytes and does not call the model/provider transport. A
known sent outcome whose Sources acknowledgement was interrupted may retry that
accounting acknowledgement only.

The same high-consent reconciliation action can converge genuinely missing
cohorts without guessing. Its authorization fingerprint binds the frozen
snapshot inventory, enrollment and Sources-archive digests, resolved paths,
disclosure outcomes, explicit Truth targets or quarantine identities,
missing-cohort quarantine choices, and deferred effect IDs. Marker, archive,
or scope drift fails closed. Within that exact approved scope it may:

- reconstitute a missing local identity from sanitized stable enrollment fields
  into the current schema, with no bootstrap/session/CSRF/gesture rows;
- reconstitute a missing Sources authority only from an exact authorized
  same-authority archive, restoring usages/redaction history while pausing
  nonterminal effects as `imported_inert`;
- import an identity-bound portable Truth ledger and causality envelope
  together into an explicit existing Folder target, or quarantine an
  unavailable permanent Truth-store identity;
- reconstitute an absent Journal or conversation cohort as empty unknown-
  provenance state only when its database is absent, retaining quarantine
  receipts; and
- defer an exact imported Sources effect until a later fresh high-consent
  `recover_effect` authorization binds its payload digest for 15 minutes and
  returns delivery to the ordinary dispatcher.

There is no automatic Sources import, inferred Truth destination, effect
replay, identity-credential restore, or repair of existing incoherent state.
Missing required input or an unavailable unquarantined Truth store keeps the
system fenced.

If any check remains partial, missing, mismatched, ambiguous, or unsettled, the
marker remains. Successful reconciliation atomically moves the exact marker to
`db/source_foundation_restore_reconciled/` as an audit receipt rather than
deleting the evidence.

## Operational invariant

There is no force-clear flag. Recovery work must first converge through its
own domain contract. That keeps the system honest about what it knows after a
partial restore and prevents “absence means not sent” or “current bytes imply
historical provenance” inferences.
