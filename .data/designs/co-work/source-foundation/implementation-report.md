# Source Foundation implementation report

**Status:** As-built implementation candidate reconciled against the frozen
implementation tree. This report describes code and focused-test evidence
present in the implementation. It does not
authorize a migration, open a model-egress gate, or claim an unrun full-suite
result.

## Delivered vertical slices

| Slice | Delivered foundation | User-testable seam | Default posture |
|---|---|---|---|
| Sources and Journal capture | A Sources SQLite/blob authority with exact occurrence identity, representations, provenance, observations, derivations, access bindings, usage reservations, outbox effects, redaction, and portable export/import; authenticated loopback capture; production Journal provider and idempotent Log/Running Notes materialization | Submit Journal Quick Capture, reload, observe durable `Saved` separately from optional processing, and retry materialization without duplicating the occurrence | Exact capture/materialization is available; Journal smart processing is off |
| Source-backed Truth | Truth v9 source provenance and independent actor roles; atomic evidence/span/relation/derivation composition; Work Buddy conversation-message sources; separate candidate and lifecycle decisions; source usage reconciliation; durable, authorized Truth-to-Hindsight projection | Propose a claim from an exact conversation message or retained source, inspect its source/producer/decision provenance, then make a separate confirm/challenge/reject decision | Hindsight projection is off until a matching durable authorization exists |
| Co-work causality | A DOM-free TypeScript document kernel sharing the React schema/Markdown semantics; packaged worker protocol; Python authorization, source equality, CAS, causality, binding, change, projection, redaction, and recovery services; minimal origin/source inspection UI | Open a source-backed document, inspect its origin, apply an authorized source-backed change, and follow a domain projection from the committed document head | No claim of arbitrary per-character authorship; major UX work is deferred |
| Journal and task-note migration | Authority-aware adapters; reviewed inventory and shadow import; exact/normalized parity; per-entity epochs; controlled cutover/rollback; latest-head projection; divergence capture/pause; current Journal exit evidence; idempotent task-note lifecycle sagas | Inventory and shadow one entity, inspect parity and authority, rehearse projection/divergence/rollback, and certify current Journal exit evidence without changing the user's vault cohort wholesale | Journal and task-note cutovers ship closed; task-note append under Co-work authority fails closed |

The branch also adds a high-consent Sources maintenance surface for preview,
export, import, redaction, interrupted-export recovery/abort, and effect
recovery. Imports keep foreign authorities quarantined by default and imported
operational effects inert until separately reauthorized.

## Authority and provenance guarantees

- Sources owns exact retained occurrences and access to them. Truth, Co-work,
  Journal, and Tasks continue to own their domain meaning and lifecycle.
- A trusted local submission proves the exact payload, local inputter, and
  context-bound gesture accepted by that surface. It does not prove who
  composed pasted, imported, dictated, or otherwise supplied words.
- The dashboard human-authority path uses a persistent installation identity,
  a trusted-host-minted one-time bootstrap, a revocable browser session/CSRF
  boundary, and one-time action/subject/context-bound gestures. There is no
  HTTP endpoint that mints bootstrap authority. Unsupported remote human
  authority remains unavailable rather than degrading to request headers.
- Truth records semantic producer/reviser, source or evidence selector,
  candidate decider, mutation applier, substantive reviewer, lifecycle
  decider, and attester independently. Accepting AI-prepared work does not
  relabel it as human-authored.
- Co-work records the exact before/after heads and the assurance actually
  established by the kernel, persistence, projection, trusted surface, or
  attestation. An opaque Yjs update is not treated as character-level proof.
- Conversation ingress retains the local inputter/gesture provenance when it
  exists while leaving message authorship unknown unless a separate basis
  establishes it.

## Disclosure and redaction guarantees

Agent Execution owns content-free run/disclosure manifests. A source-bearing
handoff reserves the live source/redaction epoch, records `possibly_sent`
before the transport boundary, and binds the input manifest to the resulting
output. Truth analysis, Co-work workers, Journal smart processing, and
Truth-to-Hindsight projection use that boundary. Dynamic search/fetch material
is captured as a source and retains its lineage rather than becoming an
untracked prompt copy. An ambiguous send is never automatically replayed; it
requires a proven outcome during recovery.

Managed readable copies register a source usage before content is released and
acknowledge the resulting copy or semantic derivative. Redaction removes the
retained readable representation, advances its redaction epoch, and dispatches
idempotent cleanup to registered consumers. Exact managed copies can be
scrubbed; semantic derivatives remain explicitly `review_required` when safe
automatic removal cannot be established. Truth evidence/document content,
Co-work projections, Journal/task-note managed sections, conversation-linked
copies, and Hindsight projection state participate through their domain
contracts.

This is application-level managed-copy deletion, not a promise of global
erasure. Previously issued offline exports or backups cannot be recalled.
Portable exports therefore carry their declared source/redaction history, and
the operator warns that creating an archive creates a separately retained
copy.

## Backup, restore, and reconstitution guarantees

The ordinary machine backup does not silently place retained Sources content,
Journal capture content, or browser authority secrets into an unencrypted
archive. Scoped Truth recovery instead carries identity-bound ledger and
document-causality companions. Restore hot-copies the sensitive retained live
databases and Sources tree into staging, so it never removes a live authority
before the final database swap; portable Truth payload remains available only
behind the resulting fence.

Every machine restore publishes a durable read-only Source Foundation fence in
the validated staging tree before the atomic database swap. While that marker
exists, source/domain writes, disclosure dispatch, document changes,
migrations, Hindsight work, and relevant filesystem-only Truth/Y.Doc mutation
boundaries fail closed. A read can reopen only an existing schema/identity it
can validate; missing state is not silently created under the fence.

The restore operator binds fresh high consent to the marker's immutable
inventory and exact recovery inputs. It can:

- reconstitute only stable local enrollment fields from a digest-bound
  sanitized record, never browser sessions, bootstrap tokens, CSRF tokens, or
  gestures;
- rebuild a missing Sources authority from an exact authorized portable
  archive, with operational effects paused;
- import a scoped Truth ledger together with its matching causality envelope
  into an explicitly named existing Folder target that is empty or already an
  exact match, or explicitly quarantine that permanent store identity;
- explicitly reconstitute an absent Journal or conversation cohort as empty
  state with unknown provenance while retaining a quarantine receipt; and
- defer exact imported Sources effects for later operator reauthorization.

Reconciliation cross-checks identity, source bytes and reservations,
disclosure outcomes, the required reverse dependency for every retained
user/agent conversation message, Journal/task authority epochs and projections
against registered Truth causality, Truth registry/store/causality identity,
incomplete changes, and Hindsight delivery/cleanup. Consent is fingerprinted
to the frozen inventory, exact input digests and resolved paths, outcomes,
targets, quarantine choices, and deferred effect IDs; marker or archive drift
fails closed. `possibly_sent`, missing, mismatched, partial, unavailable but
unquarantined, or `review_required` state keeps the fence in place. There is no
force-clear; success atomically archives the marker as an audit receipt.

## Closed rollout gates

- `journal.content_migration.enabled: false`
- `journal.content_migration.cutover_enabled: false`
- `journal.smart_processing.enabled: false`
- `hindsight.truth_projection.enabled: false`, plus a required exact durable
  authorization whose store, policy, recipient, provider, and model fields
  match runtime configuration
- task-note migration is inactive unless `task_note_migration.enabled` is
  explicitly true; its task-note and Journal prerequisite cutover gates begin
  closed in durable state
- non-loopback/Tailscale human-authority writes remain disabled until a
  verified remote principal provider exists
- imported Sources effects remain inert after portable import/reconstitution;
  release requires fresh high-consent authorization bound to one exact payload
  digest, expires after 15 minutes, and leaves delivery to the ordinary
  dispatcher

Opening an adapter flag does not move an entity's authority. Journal and task
notes require a reviewed shadow, current parity, an explicit per-entity
cutover, and a still-open rollback deadline; task-note cutover additionally
requires current Journal exit evidence. Restoring a backup does not open any
of these gates.

## Validation evidence at this checkpoint

Post-freeze validation was split into bounded domain lanes so a slow Windows
test process could not turn a timeout into a false pass. Counts are reported
per lane and are not summed because a small number of cross-domain integration
tests intentionally appear in more than one lane:

- Sources, local identity, disclosure, restore/reconstitution, conversation
  provenance, and related capability invariants: 193 Python tests passed.
- Journal, task-note, document-kernel, collectors, email, context/IR, health,
  and cleanup compatibility: 1,375 Python tests passed.
- Truth v9, source-backed claims, Co-work Truth analysis, Hindsight projection,
  Verify, Chat, conversation dependencies, and related APIs: 626 Python tests
  passed.
- Changed React tests: 17 files / 139 tests passed, including the
  document-kernel runtime; TypeScript type-check passed.
- The production React build passed. The document-kernel production build and
  two-build determinism check passed.
- Python wheel and source distribution builds passed. Each archive contains
  only `work_buddy/document_kernel/runtime_dist/worker.mjs` under the packaged
  kernel runtime and no dashboard manifest or icons.
- Agent documentation validation passed across 505 units with zero failures;
  its 43 advisory warnings are pre-existing durable-surface or unavailable
  optional-memory-operation warnings. The knowledge index rebuild succeeded.
- Python compile checks and `git diff --check` passed.

An initial full React invocation exceeded the host timeout without a result and
is not counted; every changed React test was then run successfully in bounded
batches. This report does not claim that the entire repository test suite ran
as one process.

## Explicit follow-ons

- Complete the larger Truth/Co-work/Journal interaction and information-
  architecture pass now that authority and provenance have stable seams.
- Add exact current-turn Claude Code/Codex or other harness providers only
  through trusted host bridges; never infer identity from “latest message,”
  turn index, or text search.
- Add a verified remote/multi-user principal provider before enabling remote
  human-authority decisions.
- Implement crash-safe source-backed append for Co-work-authoritative task
  notes before opening that migration gate.
- Add broader provider rechecking, third-party connectors, typed task/schedule
  effects, and retention policy incrementally through the Sources provider and
  usage contracts.
- Do not infer historical authorship, universal source verification,
  per-character collaborative authorship, or retroactive erasure from the new
  foundation.

The operational next step is validation and an intentionally small pilot—not
a blanket authority cutover. Inventory, shadow import, parity, divergence,
rollback, redaction, export/import, restore, and process-restart behavior should
be rehearsed on disposable entities before any production gate changes.
