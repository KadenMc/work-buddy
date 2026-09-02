---
name: Running Note Lifecycle
kind: concept
description: Mutable atomic Running Notes model and dashboard/provider responsibilities.
summary: Running Notes are editable native Journal entries with stable identity, Source-backed provenance, and lifecycle semantics; they are not immutable records or post-and-forget captures.
tags:
- journal
- running-notes
- editing
- tombstones
- lifecycle
aliases:
- mutable Running Notes
- running note entries
parents:
- journal
dev_notes: |-
  The production `HttpJournalProvider` preserves IDs and versions through the native Journal store, supports mutation idempotency, and tombstones deletions. The React in-memory provider remains for fixtures only. Imported Markdown is historical evidence: migration assigns durable identities and records exact Source references, while unknown authorship or review state stays explicitly unknown. Never mint line-position IDs.
---

Running Notes are mutable atomic entries in the Journal SQLite authority. They differ from records: a record represents something that already happened, while a Running Note remains working material that the user may refine or remove.

## Entry contract

Each entry has stable identity, versioned text content, ordering/time metadata, authorship and review state, and an exact Source reference for every accepted write. The UI supports edit, save, and delete. Save uses the expected version so a stale client cannot silently overwrite a newer edit. Client mutation IDs make retries idempotent.

Delete creates a tombstone in the durable provider rather than erasing audit history. Tombstoned entries disappear from the normal collection but remain recoverable by backend lifecycle tooling.

## UI and provider ownership

The widget owns editing interaction and temporary drafts. The Journal provider owns validation, conflict detection, persistence, and tombstone behavior. The dashboard's draft runtime protects unsaved text across refreshes; it does not replace the provider's durable note store.

Provider capability is explicit. A read-only compatibility provider disables or omits mutation actions. A fixture/in-memory provider remains visibly non-durable and never masquerades as live persisted data after a provider failure.

The Source-first capture coordinator and tombstone store implement this contract.
Legacy daily-note text can enter only through an explicit, deterministic import
cohort. The frozen files remain historical migration evidence; they are never a
post-seal projection or a live write target.

An individual Running Note may bind its content authority to a domain-bound
Co-work document. Provenance and Truth are separate controls: provenance remains
mandatory, while the document's Truth activation may be `disabled`, `enabled`,
or `paused`. Changing Truth activation is explicit and does not alter the note's
identity or discard its provenance history. The Co-work Truth settings control
refreshes the authoritative policy and document head when opened, requires a
separate confirmation, and submits the displayed activation revision,
interaction-contract digest, document-head digest, unique intent, and local
human gesture. Documents with ledger history can be paused but not silently
disabled; no field or agent action promotes a document automatically.

See `journal/running_notes`, `journal/source-backed-capture`,
`cowork/source-backed-documents`, `services/dashboard/react/widget-platform`,
and `services/dashboard/react`.
