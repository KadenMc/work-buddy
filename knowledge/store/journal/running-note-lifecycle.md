---
name: Running Note Lifecycle
kind: concept
description: Mutable atomic Running Notes model and dashboard/provider responsibilities.
summary: Running Notes are editable atomic Markdown entries with stable identity and lifecycle semantics; they are not immutable records or post-and-forget captures.
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
  The production `HttpJournalProvider` now preserves IDs and versions through the Journal capture store, supports mutation idempotency, and tombstones deletions. The React in-memory provider remains for fixtures only. Preexisting unmarked Markdown lines remain a separate read-only compatibility projection until migration assigns reviewed stable identities; never mint line-position IDs.
---

Running Notes are mutable atomic Markdown entries. They differ from records: a record represents something that already happened, while a Running Note remains working material that the user may refine or remove.

## Entry contract

Each entry has stable identity, Markdown content, ordering/time metadata, and a version. The UI supports edit, save, and delete. Save uses the expected version so a stale client cannot silently overwrite a newer edit. Client mutation IDs make retries idempotent.

Delete creates a tombstone in the durable provider rather than erasing audit history. Tombstoned entries disappear from the normal collection but remain recoverable by backend lifecycle tooling.

## UI and provider ownership

The widget owns editing interaction and temporary drafts. The Journal provider owns validation, conflict detection, persistence, and tombstone behavior. The dashboard's draft runtime protects unsaved text across refreshes; it does not replace the provider's durable note store.

Provider capability is explicit. A read-only compatibility provider disables or omits mutation actions. A fixture/in-memory provider remains visibly non-durable and never masquerades as live persisted data after a provider failure.

The durable source-first capture/provider and tombstone store implement this
contract for newly captured Running Notes. Existing unmarked legacy notes stay
read-only until the explicit migration/parity gate assigns stable identities.
An individual Running Note may cut its content authority to a domain-bound
Co-work document; after cutover, its Markdown block is a non-clobbering
compatibility projection guarded by an authority epoch and base hashes.

See `journal/running_notes`, `journal/source-backed-capture`,
`cowork/source-backed-documents`, `services/dashboard/react/widget-platform`,
and `services/dashboard/react`.
