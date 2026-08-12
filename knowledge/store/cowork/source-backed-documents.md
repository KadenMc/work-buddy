---
name: Source-backed Co-work documents
kind: system
description: Domain bindings, headless structured-document kernel, durable change records, projection recovery, and source-dependency maintenance for Co-work documents.
summary: A domain entity can own one Co-work document through an explicit authority epoch. The shared headless kernel produces canonical Markdown/Yjs mappings; Python verifies hashes/CAS/protocol binding and records every accepted mutation before compatibility projection.
entry_points:
- work_buddy.document_kernel
- dashboard-react/src/apps/cowork/document-kernel
- work_buddy.document_kernel.causality
- work_buddy.document_kernel.cowork_integration
- work_buddy.document_kernel.journal_projection
tags:
- cowork
- documents
- causality
- yjs
- provenance
- projections
- source-backed
aliases:
- document kernel
- domain-bound documents
- document change records
parents:
- cowork
requires:
- architecture/source-foundation
dev_notes: |-
  Python stores Yjs as opaque bytes; do not invent a second partial interpreter. The packaged DOM-free TypeScript worker is the trusted structured-document kernel. Python independently verifies request hashes, protocol/schema/runtime versions, current generation/head, update bytes, and compare-and-swap results.

  A domain binding has one current content authority and monotonically increasing epoch. Journal/task Markdown is a compatibility projection after cutover, not peer authority. External divergence pauses projection and is captured as an exact file-origin source for review.

  Never append a normal browser update to a bound document without a `DocumentChangeRecord`. The durable intent/materialized/committed state is the recovery authority. The `change_id` query link opens a compact source-and-change inspection row; do not expose raw source bytes through that read endpoint.
---

# Source-backed Co-work documents

`work_buddy.document_kernel` provides a production-packaged, DOM-free worker
using the same ProseMirror/Yjs schema and Markdown mapping as the browser. It
supports bootstrap, projection, source Markdown application, text replacement,
and opaque update validation. Build tests require a deterministic worker and
verify that no dashboard public assets leak into its package.

The per-store causality database records domain bindings, authority epochs,
change intents and immutable change records, projection intents/receipts/
cursors, and deterministic export/import. A prepared operation survives a
process crash; a committed document head cannot exist without recoverable
causality.

The first real pilot is one Journal Running Note:

1. resolve and reserve its exact retained source;
2. create the bound Co-work document and initial source-backed change;
3. cut that note's authority epoch to Co-work;
4. project the canonical head into its managed Journal section;
5. record the reverse navigation/inspection mirror;
6. reconcile direct editor updates and missed projections after restart.

The file-import source provider is root-confined, no-follow, UTF-8 Markdown,
bounded, and exact-whole-file only. It powers divergence capture without
claiming file authorship. Task-note migration reuses the same kernel and
authority model behind closed per-note rollout gates.
