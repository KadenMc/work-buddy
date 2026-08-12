---
name: Source-backed Journal capture
kind: system
description: Durable Quick Capture path that commits exact input to Sources before Journal routing or optional processing.
summary: Journal Quick Capture is now a real source-first write. Exact input and a reference-only command/outbox commit atomically; Journal materialization, routing, and processing reconcile independently, so a failed effect cannot lose or masquerade as a failed save.
entry_points:
- work_buddy.journal_capture.api
- work_buddy.journal_capture.service
- work_buddy.journal_capture.smart
- work_buddy.journal_capture.dispatch
- work_buddy.journal_capture.store
- dashboard-react/src/apps/journal/providers/HttpJournalProvider.ts
tags:
- journal
- capture
- sources
- quick-capture
- running-notes
- recovery
aliases:
- real Journal capture
- durable Quick Capture
parents:
- journal
requires:
- architecture/source-foundation
dev_notes: |-
  Preserve the established payload: `target_id = auto | log | running_notes`, `mode = dumb | smart`, exact text, input mode, optional stated time, and client mutation ID. There is no `both` target. `auto + dumb` is invalid.

  A persistence acknowledgement means Sources and the reference-only command/outbox committed. It does not mean optional smart processing succeeded. The UI clears its draft only after persistence and presents delayed/failed processing separately.

  Stable hidden entry markers make Markdown compatibility writes occurrence-safe: two identical entries remain distinct, and a crash after file write but before SQLite acknowledgement reconciles without appending again.
---

# Source-backed Journal capture

The production Journal provider sends Quick Capture to
`POST /api/journal/captures`. The protected route derives the actor from the
enrolled local session and consumes a one-use gesture bound to the exact
semantic request. It accepts no trusted actor fields from the caller.

Sources atomically records the exact representation, ingress submission,
versioned Journal command, and source outbox entry. Journal then creates its
capture/effect records and materializes the selected destination through
`JournalContentAdapter`. A recovery sweep drains missed outbox work on startup
and authoritative Journal reads.

Direct Log and Running Note captures need no model. `auto` routing and optional
smart annotation are separate effects. The production Smart processor is
disabled by default under `journal.smart_processing`; when explicitly enabled,
it uses the configured concrete frontier tier, displays the provider/model
boundary through the Journal capability description, and records a content-free
Agent Execution disclosure manifest before the exact retained capture reaches
the model. The model boundary is capped at 32 KiB and never silently truncates;
larger captures remain saved while optional processing fails visibly. It has no
tools or web access, never rewrites the saved text, disables
model-response caching, and binds its structured result back to the Journal
capture. A settled result is never automatically sent again.

New Running Notes have stable entry IDs, versions, and tombstones. A user can
open one in Co-work, which creates a domain-bound structured document from the
same exact source, records the initial change and actors/assurances, cuts that
note's content-authority epoch to Co-work, and projects later accepted heads
back into the managed Journal section without clobbering external divergence.

Legacy Journal Log and Running Notes content now use the same compatibility
adapter during migration. Authority advances independently for one logical-day
Log or one stable Running Note. The document-kernel binding carries the
canonical epoch; Markdown remains authoritative before cutover and becomes a
section-CAS compatibility projection afterward. Unmarked legacy prose remains
read-only until an operator assigns an opaque identity to an explicitly
reviewed occurrence. The Journal migration operator, closed deployment gates,
and derived exit-evidence contract are documented in
`journal/journal-content-migration-operator`.
