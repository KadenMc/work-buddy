---
name: Truth
kind: concept
description: Scoped, provenance-aware claim stores with append-only history, human confirmation authority, deterministic recovery, and integrity enforcement.
tags:
- truth
- claims
- provenance
- evidence
- confirmation
- sqlite
- append-only
- integrity
aliases:
- truth architecture
- truth engine
- truth store
- truth layer
parents:
- architecture
dev_notes: |-
  `work_buddy/truth/` is the canonical code namespace. "Truth layer" is the design and discovery umbrella alias, not a second product or package name.

  Base records and status history are append-only. Do not add direct update or delete paths; redaction is the only sanctioned mutation and its triggers admit only the exact content-removal shape while retaining identity and hashes. `claims_current` is a disposable projection and must remain rebuildable from durable history without adding ledger events.

  Writers use `BEGIN IMMEDIATE`. The ledger's engine-assigned sequence is the canonical append order; imported or caller-provided ordering is untrusted. Profile changes constrain new writes but do not retroactively invalidate existing history.

  The post-commit hook owns recovery export publication and observer notification after the database commit. MCP and CLI lifecycle event publication also happens only after commit. An event publication failure is surfaced without rolling back the authoritative Truth transaction. A failed recovery export removes any stale sidecar rather than leaving it behind as if it represented the committed state. Import stages a temporary store, validates its identity, hashes, links, blobs, and integrity findings, reproduces the canonical export, then publishes the sidecar atomically.

  Cross-store `wb-truth:` references fail closed for confirmation when their authority cannot be resolved locally. Integrity reporting may retain unresolved external references as warnings, but it must not silently promote them to confirmed premises.

  The executable contract inventory is `tests/unit/truth/INVARIANT_COVERAGE.md`; declarative end-to-end workloads live in `tests/fixtures/truth/`. Released schema fixtures are immutable compatibility inputs: add a new versioned fixture instead of regenerating an old one.
---

Truth is work-buddy's durable kernel for recording what a claim says, which evidence supports it, who or what produced it, what a human decided, and how that state changed over time. The canonical implementation namespace is `work_buddy.truth`. The phrase **truth layer** remains a design and discovery alias rather than a separate canonical subsystem name.

Truth ships as a Python library kernel, twelve MCP capabilities, and a direct CLI surface. Both transports delegate to `work_buddy.truth` rather than reproduce its authority, lifecycle, or integrity rules.

## Transport surfaces

The MCP surface provides the following capabilities:

- Store inventory: `truth_store_create`, `truth_store_list`.
- Evidence: `truth_evidence_capture`, `truth_span_mark`.
- Claim lifecycle: `truth_claim_propose`, `truth_claim_confirm`, `truth_claim_reject`, `truth_claim_challenge`, `truth_claim_supersede`, `truth_claim_redact`.
- Read and maintenance: `truth_query`, `truth_sweep`.

The direct shell surface is `wbuddy truth`. It has exactly five verbs: `capture`, `propose`, `query`, `confirm`, and `migrate`. The CLI opens the nearest `.wb-truth/` sidecar, or an explicitly selected store, without requiring the MCP gateway to be running. Interactive confirmation is a local-human surface only. A detected agent context cannot mint the human gesture even when it allocates a TTY, and must use MCP per-invocation consent or a still-valid gesture minted by a human. As with direct SQLite access, a local process that deliberately strips its agent identity remains inside the documented owning-machine trust boundary.

Portable store import is library-only through `work_buddy.truth.export.import_store`. No `truth_store_import` MCP capability or sixth CLI verb is registered. Portable recovery consumers call the kernel import seam explicitly.

## Store topology

Each participating scope root owns one `.wb-truth/` sidecar. A sidecar contains a permanent store identity and profile in `store.yaml`, the working SQLite database in `store.db`, optional content-addressed blobs, and the deterministic recovery stream at `export/claims.jsonl`. Truth stores therefore travel with the project or purpose directory they describe. They are not another shared claim database under work-buddy's configured data root.

The machine registry at `<data_root>/db/truth_registry.db` records known sidecar paths, permanent store IDs, profiles, titles, last-seen timestamps, and reachability. It is an inventory and health index, not a second source of Truth. Registry access revalidates reachable rows against the sidecar, and duplicate reachable paths for one permanent store identity fail closed.

`store_id`, record IDs, `wb-truth:` URIs, and append history survive migration and export/import recovery. Store creation reserves a previously absent sidecar, registers it, and compensates partial state if registration fails. It refuses a pre-existing sidecar before mutation. CLI or MCP access refreshes a registered store's observation without moving authoritative content into the machine database.

## Evidence, claims, and history

The durable model records evidence and addressable evidence spans, claims, typed links, derivations, review gestures, and append-only status events. Engine-assigned ledger sequence provides one global transaction order. Valid time, transaction order, and human decision time remain distinct so historical and as-of queries do not depend on a mutable current-state row.

`claims_current` is a rebuildable projection over durable history. It may be discarded and deterministically regenerated without changing the ledger. Conflicts, supersession, retraction, review requirements, source integrity, and target fingerprints are derived without erasing the facts that led to the current view.

## Authority and confirmation

Agents may capture evidence and propose claims, but they cannot confirm their own claims. Confirmation requires a human actor and a server-composed, single-use gesture bound to the exact subject, canonical payload, displayed context, confirmation surface, and validity window. On the MCP surface, confirmation, rejection, and redaction use the same per-invocation authority boundary. Each MCP approval can authorize only the exact reviewed invocation, is consumed before the guarded function executes, and creates no reusable consent grant. Existing individual, workflow, session, or time-window grants cannot carry into these decisions.

For MCP agent writes, the gateway-injected session identifier selects a session manifest; the durable session identity and harness come from that manifest, with placeholder values rejected. A non-placeholder manifest model is authoritative and the required `producer_model` argument must match it. When the manifest has no usable model, the required caller value is retained but recorded as `model_source=caller_asserted`, explicitly not authenticated; a manifest-backed value is recorded as `model_source=session_manifest`. The engine still assigns trust class and prevents callers from overriding these producer fields, so agent content cannot be laundered into human provenance.

Premise authority follows the weakest link: every required premise must resolve as confirmed before a dependent claim can be confirmed. Competing confirmed successors fail closed rather than silently branching canon. Rejection remains reason-classed so falsehood, preference, and plain rejection retain different durable consequences.

## Lifecycle events

Represented lifecycle transitions publish durable `truth.*` events after the Truth transaction succeeds. The vocabulary is `truth.store_created`, `truth.evidence_captured`, `truth.span_marked`, `truth.claim_proposed`, `truth.claim_confirmed`, `truth.claim_rejected`, `truth.claim_challenged`, `truth.claim_superseded`, `truth.claim_redacted`, and `truth.sweep_completed`. Deduplicated or otherwise unchanged outcomes emit no event. A confirmation attempt that fails closed into `needs_review` also emits no misleading `truth.claim_confirmed` event because no separate event is defined for that status.

These events are an observer surface, not a write authority. Publication results are returned to the caller, but a dispatcher failure never rolls back or changes the committed Truth state.

## Integrity and portability

Integrity sweeps and as-of queries read the durable ledger rather than trusting the projection. Errors represent conditions that make publication or recovery unsafe; portable warnings preserve unresolved conditions that may require another store or later human review.

`claims.jsonl` is a deterministic, lossless recovery format, not a human-facing projection. Export preserves stable IDs, store identity, append order, and referenced blobs. Import validates the entire stream in a staged sidecar, upcasts supported older formats, rebuilds derived state, and atomically publishes only after the staged store reproduces the validated recovery export.

The machine backup pipeline discovers scoped stores through the registry. For each reachable store it stages `truth_stores/<store_id>/store.yaml` and `truth_stores/<store_id>/claims.jsonl`. The manifest records included, unreachable, and errored stores explicitly. Backups never copy a scoped store's live `store.db`, because the deterministic portable export is the recovery contract.

## Redaction

Redaction is the only sanctioned exception to immutable base content. It removes protected content while retaining record identity, hashes, ledger history, and content-free recovery markers. Claim redaction appends a terminal co-status so a redacted claim cannot remain a confirmed current fact. Evidence redaction removes dependent quoted material and deletes a blob only after its final live reference disappears.

## Schema evolution

Every store migrates on open. An older engine refuses a newer store before mutation or snapshotting. Each version bump snapshots the prior database before applying its transaction, and released-schema fixtures remain frozen compatibility evidence. Migrations preserve ledger history and permanent identity; profile tightening affects future writes without retroactively rewriting accepted history. Older supported JSONL formats provide the recovery escape hatch by upcasting into the current engine before rebuild.
