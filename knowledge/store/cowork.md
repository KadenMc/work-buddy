---
name: Co-work
kind: concept
description: The Folder-based human and agent working surface for living documents, with durable editing, explicit file writes, proposal review, and scoped provenance.
summary: A user opens an ordinary Folder, sets it up for Co-work under .wbuddy/cowork, and creates or registers Markdown documents without defining a separate project type. Co-work restores the selected Folder and document from the URL, keeps structured editing state durable through an offline-capable outbox, writes Markdown only through an explicit human action, detects outside file changes before overwrite, and routes agent contributions through human-reviewed proposals.
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
The unit a user chooses is simply a **Folder**. Its displayed name is the
directory name; Co-work does not make the user define a second project object or
select a predefined document type. A completely ordinary Folder can be inspected
without mutation and then explicitly set up for Co-work.

Setup creates `.wbuddy/manifest.yaml` for work-buddy-level metadata and the
canonical Co-work store at `.wbuddy/cowork/`.

Because the dashboard has no authentication and Co-work can read host files,
all `/api/truth/cowork/*` and `/api/truth/doc/*` routes reject non-loopback
callers. Remote Co-work remains unavailable until Work Buddy has an
authenticated remote surface; a network-bound dashboard must not expose Folder
contents by implication. The guard also requires a local browser-visible Host
and rejects forwarding/Tailscale proxy markers, because a loopback reverse
proxy is not itself proof of a local user.

## Folder and document flow

With no Folder open, the surface presents a Folder launcher rather than an empty
editor. A user can choose a Folder with the native picker or enter a path. Co-work
then shows the Folder's document catalog and supports three deliberate starts:

- create a new Markdown document;
- register an existing Markdown file; or
- begin in a scratch document and promote it into the Folder later.

Folder and document selection are encoded in the URL, so reload, history
navigation, and a shared local link restore the same working context. Document
selection does not imply a file write. Retiring a document removes it from the
active catalog while preserving its Markdown file and durable history.

## Editing and persistence

A Co-work document has two related representations:

- the authoritative structured collaborative head used by the editor, with
  monotonic versions and compare-and-swap protection; and
- the Markdown file in the selected Folder, which is materialized only through
  the explicit **Save Markdown** action.

Local edits enter an IndexedDB outbox before transport. The provider can reload
them after a browser refresh or temporary disconnection and acknowledges them
only after the server durably accepts them. Opening or reimporting a document
uses a durability barrier and one atomic model commit, so a delayed request
cannot replace a newer navigation choice.

Each outbox entry carries the document's stable logical Y.Doc generation. Normal
snapshot compaction changes the cursor epoch but preserves that generation, so
an offline edit can replay safely after another tab compacts. An explicit
structured replacement rotates the generation and fails closed instead of
combining pre-replacement updates with the new document. Pushes carry the
generation as a server-checked precondition under the document lock, so an
opaque update cannot cross a replacement boundary even when snapshot bytes
happen to hash identically.

Before writing Markdown, Co-work compares the registered file fingerprint with
the current file. An outside change blocks overwrite and offers an explicit
reimport path. Reimport prepares the catalog and structured head off-model, then
replaces the visible document atomically; failure leaves the current editor
intact. An ambiguous commit response is retried with the exact retained
idempotent payload rather than rereading staged source that may already have
been consumed. If another tab retires the active document, catalog
reconciliation first makes local edits device-durable and then revokes the
writable session. Recovery and quarantine paths fail closed when persisted
state cannot be validated.

## Human and agent authority

The agent-facing capabilities are `cowork_doc_list`, `cowork_doc_get`,
`cowork_doc_propose_edit`, `cowork_doc_comment`, and `cowork_doc_expression_mark`.
An agent reads a document and proposes work on it. Every agent contribution is an
open proposal, never a decision. Accept, amend, reject, redirect, endorse, and
defer are human gestures collected on the dashboard, because an agent cannot
approve its own content.

The review rail groups proposals into a sitting so the user can decide them in
context. The document's Markdown file is written through the materialization
engine, never directly by an agent, and the claims a document expresses live in
the Folder's scoped Truth ledger through expression links. Internally, the
engine still uses terms such as scope root, store ID, and Truth store; the
dashboard consistently calls the thing the user selected a **Folder**.
