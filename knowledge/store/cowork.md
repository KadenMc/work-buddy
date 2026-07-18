---
name: Co-work
kind: concept
description: The human and agent working surface for living documents, where agent contributions are proposals a human decides and the document's truth rests on the scoped ledger.
summary: Co-work is where a person and an agent co-author a registered living document. Agents may read a cowork doc and propose quote-anchored edits, flags, and claim links. Only a human gesture accepts, amends, rejects, redirects, endorses, or defers. The document's file is written by materialization, never by an agent, and the claims inside it live in the scoped truth ledger.
tags:
- cowork
- documents
- proposals
- human-in-the-loop
- surface
aliases:
- cowork surface
- cowork docs
- co-work
- document surface
- living documents
---

# Co-work

Co-work is work-buddy's surface for co-authoring living documents with an agent.
A cowork doc is a registered file whose structured content, review layer, and
provenance are managed by the surface. Co-work is where you work, and truth is
what the work rests on.

The agent-facing capabilities are `cowork_doc_list`, `cowork_doc_get`,
`cowork_doc_propose_edit`, `cowork_doc_comment`, and `cowork_doc_expression_mark`.
An agent reads a document and proposes work on it. Every agent contribution is an
open proposal, never a decision. Accept, amend, reject, redirect, endorse, and
defer are human gestures collected on the dashboard, because an agent cannot
approve its own content.

A scope admits documents only when its store profile enables the
`document_surface` block. The document's Markdown file is written through the
engine at materialization, never by an agent, and the claims a document
expresses live in the scoped truth ledger through expression links.
