---
name: Co-work
kind: concept
description: The Folder-based human and agent working surface for living documents, with durable editing, explicit file writes, and proposal review.
summary: A user opens an ordinary Folder and Co-work prepares its .wbuddy/cowork support data automatically, without defining a separate project type. The user can create a document or add existing Markdown, while an untitled document remains saved on the device until it is given a Folder location. Co-work restores the selected Folder and document from the URL, keeps structured editing state durable through an offline-capable outbox, writes Markdown only through an explicit human action, detects outside file changes before overwrite, and routes agent contributions through human-reviewed proposals.
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
without mutation. Choosing it is the user's intent to open it: Co-work performs
the read-only checks and, when needed, creates its support data automatically.

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

With no Folder open, the toolbar's **Folder** control opens the native picker
directly. On Windows this is the Explorer-style `IFileOpenDialog`, attached to
the foreground window where possible. There is no intermediate menu, manual
host-path form, setup lecture, or user-operated inspection step. Co-work checks
and prepares the selected Folder behind the scenes, then shows its document
catalog. The compact launcher keeps **New document**, recent Folders, and
on-device documents together in the first viewport.

The user can create a new document or add an existing Markdown file. A document
started before a Folder is chosen is simply untitled and **Saved on this
device** until the user saves it into a Folder; `scratch` is only an internal
persistence term, not a separate user-facing document type. Additional
on-device documents are numbered (`Untitled 2`, `Untitled 3`, and so on), and
their human edits refresh an **Edited** timestamp so recovery choices remain
identifiable.

Folder and document selection are encoded in the URL, so reload, history
navigation, and a shared local link restore the same working context. Document
selection does not imply a file write. Retiring a document removes it from the
active catalog while preserving its Markdown file and durable history.

## Editing and persistence

A Co-work document has two related representations:

- the authoritative structured collaborative head used by the editor, with
  monotonic versions and compare-and-swap protection; and
- the Markdown file in the selected Folder, which is materialized only through
  the explicit **Save** action.

Local edits enter an IndexedDB outbox before transport. The provider can reload
them after a browser refresh or temporary disconnection and acknowledges them
only after the server durably accepts them. Opening or reimporting a document
uses a durability barrier and one atomic model commit, so a delayed request
cannot replace a newer navigation choice.

If device hydration fails, the editor presents a working retry action rather
than remaining in an indefinite loading state. The document bar presents one
prioritized durability/save status and only the recovery action that can
currently make progress. Rendered errors use human-facing recovery copy;
Y.Doc, snapshot, hash, generation, and other persistence details stay in
diagnostics.

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
