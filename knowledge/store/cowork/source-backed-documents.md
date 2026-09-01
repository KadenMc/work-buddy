---
name: Source-backed Co-work documents
kind: system
description: Domain bindings, headless structured-document kernel, durable change records, projection recovery, and source-dependency maintenance for Co-work documents.
summary: A domain entity can own one Co-work document through an explicit authority epoch. Native task documents and Journal document modules use no Markdown projection; managed Journal Markdown projection is legacy compatibility only.
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

  A domain binding has one current content authority and monotonically increasing epoch. Native task documents and native Journal document modules explicitly use `projection_mode=none`. Managed-section Journal Markdown projection is retained only for pre-seal compatibility and recovery; it must not write after the database-only fence. External divergence pauses only projection-enabled compatibility domains and is captured as an exact file-origin source for review.

  Task reads and IR must project the current structured head plus uncompacted Yjs updates, not only the compacted blob. Restore preserves retired bindings/documents and creates a new active successor. Opaque local-file handles never expose absolute paths to the browser.

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

Tasks established the projection-free production path:

- each task owns at most one active Co-work binding;
- task documents use `projection_mode=none` and never render Markdown;
- reads, excerpts, email append, and IR consume the current Yjs head;
- delete/restore preserves retired history and creates a successor binding;
- local assets remain in place behind opaque, host-authorized links, with an
  explicit metadata recheck that never returns a path.

The database-only Journal keeps ordinary fields and items in its native SQLite
domain. A Journal profile may also include an optional day-scoped document
module. On explicit user intent, that module:

- binds against the persisted composition for that Journal day;
- provisions an empty Co-work document with Co-work authority from creation;
- uses `projection_mode=none`, so it never renders into legacy Journal Markdown;
- starts provenance tracking immediately under the working-document contract;
- starts Truth disabled while preserving an explicit later activation path.

## Historical Journal projection pilot

Before the database-only cutover, a Running Note could use this managed-section
compatibility sequence:

1. resolve and reserve its exact retained source;
2. create the bound Co-work document and initial source-backed change;
3. cut that note's authority epoch to Co-work;
4. project the canonical head into its managed Journal section;
5. record the reverse navigation/inspection mirror;
6. reconcile direct editor updates and missed projections after restart.

That sequence remains only to inspect or recover pre-seal bindings and receipts.
It must not project or rewrite Markdown after the Journal database fence.

The file-import source provider is root-confined, no-follow, UTF-8 Markdown,
bounded, and exact-whole-file only. It powers divergence capture without
claiming file authorship. The superseded task-note Markdown migration reused the
same kernel behind per-note gates; it is now legacy-only. See
`tasks/native-task-system`.
