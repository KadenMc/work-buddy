---
name: Truth
kind: concept
description: Scoped, evidence-backed claim stores with append-only history, human confirmation gates, source receipts, and belief revision.
summary: Truth stores keep evidence, spans, claims, derivations, document versions, lifecycle events, and exact human gestures in a Folder's canonical .wbuddy/cowork sidecar. Agents may gather and propose. Only a per-claim human decision can confirm, reject, or redact claim content.
tags:
- truth
- evidence
- claims
- provenance
- belief-revision
aliases:
- truth stores
- evidence ledger
- claim ledger
- verified knowledge
- scoped truth
parents:
- architecture/truth
---

# Truth

Truth is work-buddy's scoped evidence and claim ledger. Each store lives in a
sidecar beside the material it governs and is addressed through a machine
registry. A Folder initialized through the Co-work dashboard uses the canonical
`.wbuddy/cowork/` sidecar, and `truth_store_create` performs the same safe
Folder initialization rather than creating a second store shape.

The machine registry is discovery and health metadata, not a second claim store.
The sidecar's authoritative record is append-only except for sanctioned content
redaction. Human decisions bind one exact claim hash and its displayed receipts.

Use the `truth_*` capabilities for agent access. Use `wbuddy truth` for direct
local inspection and interactive CLI confirmation.
