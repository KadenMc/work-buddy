---
name: Truth
kind: concept
description: Scoped, evidence-backed claim stores with append-only history, human confirmation gates, source receipts, and belief revision.
summary: Truth stores keep evidence, spans, claims, derivations, lifecycle events, and exact human gestures in a local sidecar. Agents may gather and propose. Only a per-claim human decision can confirm, reject, or redact claim content.
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
project-owned `.wb-truth` sidecar and is addressed through a machine registry.
The authoritative record is append-only except for sanctioned content
redaction. Human decisions bind one exact claim hash and its displayed receipts.

Use the `truth_*` capabilities for agent access. Use `wbuddy truth` for direct
local inspection and interactive CLI confirmation.
