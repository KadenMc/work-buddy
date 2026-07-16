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
  `work_buddy/truth/` is the canonical code namespace. "Truth layer" was the design-phase umbrella term and remains an alias, not a second product or package name.

  Base records and status history are append-only. Do not add direct update or delete paths; redaction is the only sanctioned mutation and its triggers admit only the exact content-removal shape while retaining identity and hashes. `claims_current` is a disposable projection and must remain rebuildable from durable history without adding ledger events.

  Writers use `BEGIN IMMEDIATE`. The ledger's engine-assigned sequence is the canonical append order; imported or caller-provided ordering is untrusted. Profile changes constrain new writes but do not retroactively invalidate existing history.

  The post-commit hook owns publication and observer notification after the database commit. A publication failure is surfaced, and a stale recovery export is removed rather than left behind as if it represented the committed state. Import stages a temporary store, validates its identity, hashes, links, blobs, and integrity findings, reproduces the canonical export, then publishes the sidecar atomically.

  Cross-store `wb-truth:` references fail closed for confirmation when their authority cannot be resolved locally. Integrity reporting may retain unresolved external references as warnings, but it must not silently promote them to confirmed premises.

  The executable contract inventory is `tests/unit/truth/INVARIANT_COVERAGE.md`; declarative end-to-end workloads live in `tests/fixtures/truth/`. Released schema fixtures are immutable compatibility inputs: add a new versioned fixture instead of regenerating an old one.
---

Truth is work-buddy's durable kernel for recording what a claim says, which evidence supports it, who or what produced it, what a human decided, and how that state changed over time. The canonical implementation namespace is `work_buddy.truth`. The phrase **truth layer** remains a design and discovery alias rather than a separate canonical subsystem name.

The current surface is a Python library kernel. It does not yet register MCP capabilities, CLI commands, or dashboard views. Future transports must delegate to this package rather than reproduce its authority, lifecycle, or integrity rules.

## Store topology

Each participating scope root owns one `.wb-truth/` sidecar. A sidecar contains a permanent store identity and profile in `store.yaml`, the working SQLite database in `store.db`, optional content-addressed blobs, and the deterministic recovery stream at `export/claims.jsonl`. Truth stores therefore travel with the project or purpose directory they describe; they are not another shared database under work-buddy's configured data root.

`store_id`, record IDs, `wb-truth:` URIs, and append history survive migration and export/import recovery. A registry seam is accepted by import, but registry policy and product integration live outside the kernel.

## Evidence, claims, and history

The durable model records evidence and addressable evidence spans, claims, typed links, derivations, review gestures, and append-only status events. Engine-assigned ledger sequence provides one global transaction order. Valid time, transaction order, and human decision time remain distinct so historical and as-of queries do not depend on a mutable current-state row.

`claims_current` is a rebuildable projection over durable history. It may be discarded and deterministically regenerated without changing the ledger. Conflicts, supersession, retraction, review requirements, source integrity, and target fingerprints are derived without erasing the facts that led to the current view.

## Authority and confirmation

Agents may capture evidence and propose claims, but they cannot confirm their own claims. Confirmation requires a human actor and a server-composed, single-use gesture bound to the exact subject, canonical payload, displayed context, confirmation surface, and validity window. Producer identity and trust class are derived by the engine so an agent cannot launder content into human provenance.

Premise authority follows the weakest link: every required premise must resolve as confirmed before a dependent claim can be confirmed. Competing confirmed successors fail closed rather than silently branching canon. Rejection remains reason-classed so falsehood, preference, and plain rejection retain different durable consequences.

## Integrity and portability

Integrity sweeps and as-of queries read the durable ledger rather than trusting the projection. Errors represent conditions that make publication or recovery unsafe; portable warnings preserve unresolved conditions that may require another store or later human review.

`claims.jsonl` is a deterministic, lossless recovery format, not a human-facing projection. Export preserves stable IDs, store identity, append order, and referenced blobs. Import validates the entire stream in a staged sidecar, upcasts supported older formats, rebuilds derived state, and atomically publishes only after the staged store reproduces the validated recovery export.

## Redaction

Redaction is the only sanctioned exception to immutable base content. It removes protected content while retaining record identity, hashes, ledger history, and content-free recovery markers. Claim redaction appends a terminal co-status so a redacted claim cannot remain a confirmed current fact. Evidence redaction removes dependent quoted material and deletes a blob only after its final live reference disappears.

## Schema evolution

Every store migrates on open. An older engine refuses a newer store before mutation or snapshotting. Each version bump snapshots the prior database before applying its transaction, and released-schema fixtures remain frozen compatibility evidence. Migrations preserve ledger history and permanent identity; profile tightening affects future writes without retroactively rewriting accepted history. Older supported JSONL formats provide the recovery escape hatch by upcasting into the current engine before rebuild.
