---
name: Co-work
kind: concept
description: The Folder-based human and agent working surface for living documents, with durable editing, explicit file writes, and proposal review.
summary: A user opens an ordinary Folder, Co-work inspects it without mutation, and a one-time confirmation discloses the .wbuddy support data before setup. The user can create a document or start one from an existing Markdown file in place, while an untitled document remains saved on the device until it is given a Folder location. Co-work restores the selected Folder and document from the URL, keeps structured editing state durable through an offline-capable outbox, writes Markdown only through an explicit human action, detects outside file changes before overwrite, and routes agent contributions through human-reviewed proposals.
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
dev_notes: |
  Native filesystem results are treated as untrusted input even though the picker runs on the host. Scoped Markdown and destination routes resolve the selected filesystem identity, derive the returned relative path from that resolved identity, and reapply containment plus the `.wbuddy` exclusion. This is load-bearing on Windows, where an 8.3 alias such as `WBUDDY~1` can otherwise disguise the managed directory. Registered Markdown identity is case-insensitive for Windows Folder roots and case-sensitive for POSIX roots; the server remains authoritative for races and unusual Unicode identities.

  The Windows helper protocol is versioned and mode-bound. macOS cancellation returns an explicit protocol-and-mode-bound success marker; every nonzero `osascript` exit remains a genuine picker error. All picker modes share one non-blocking process lock.

  First-time setup is a focus-managed modal and disables stale document chrome from any previously active Folder. If the short-lived inspection token expires after the user confirms setup, the provider refreshes inspection and retries initialization exactly once on that same click. Do not turn that bounded retry into a loop.

  Folder, Markdown-file, and destination-Folder picker availability are distinct server capabilities and must remain distinct through the client model. Permission and availability checks are repeated at intent dispatch boundaries, not left to disabled controls alone. In read-only mode, ordinary-Folder setup is informational and cannot initialize. An on-device document remains local when the dashboard is read-only, its active Folder denies create, or it has no Folder and Folder selection is unavailable.
---

# Co-work

Co-work is work-buddy's surface for co-authoring living documents with an agent.
The unit a user chooses is simply a **Folder**. Its displayed name is the
directory name; Co-work does not make the user define a second project object or
select a predefined document type. A completely ordinary Folder can be inspected
without mutation. Choosing it is the user's intent to inspect and open it. If
the Folder has not been used with Co-work before, the dashboard pauses before
mutation and asks the user to **Set up Co-work** there.

Setup creates `.wbuddy/manifest.yaml` for work-buddy-level metadata and the
canonical Co-work store at `.wbuddy/cowork/`. The confirmation names the
`.wbuddy` support data, shows the selected host path, and states that existing
documents are not changed. Cancelling it writes nothing. An already initialized
Folder opens directly without asking again.

Because the dashboard has no authentication and Co-work can read host files,
all `/api/truth/cowork/*` and `/api/truth/doc/*` routes reject non-loopback
callers. Remote Co-work remains unavailable until Work Buddy has an
authenticated remote surface; a network-bound dashboard must not expose Folder
contents by implication. The guard also requires a local browser-visible Host
and rejects forwarding/Tailscale proxy markers, because a loopback reverse
proxy is not itself proof of a local user. Opening host UI has an additional
boundary: every native Folder, Markdown-file, and destination picker route
requires its exact Co-work intent header, rejects cross-site browser provenance
or a mismatched Origin, and the dashboard denies framing so another site cannot
place the controls in a clickjacking frame.

## Folder and document flow

With no Folder open, the toolbar's **Folder** control opens the native picker
directly. On Windows the dashboard launches a fixed
`python -I -m work_buddy.cowork.folder_picker_helper` command with only bounded,
validated mode and starting-directory arguments. That isolated PySide6 process
asks Qt for the operating system's native directory or Markdown-file dialog and
returns the selection through a small versioned JSON protocol. It does not
invoke a command shell or compile PowerShell/C# at runtime. A one-pixel Qt owner
supplies a modal/topmost ownership hint so Windows can surface the native dialog
above the browser without replacing it with a custom picker. A non-blocking
process-local lock permits one native picker at a time and returns a typed busy
response to a second request.

After read-only inspection, an ordinary Folder shows the one-time setup
confirmation; an initialized Folder opens its document catalog directly. The
confirmation is a focus-managed modal, begins on **Cancel**, and prevents
document actions from a previously active Folder while the decision is open.
When the dashboard itself is read-only, the modal instead explains that Co-work
is not set up in the Folder, offers only **Close**, and changes no files. The
document bar shows **Open document** without a dropdown caret and a single
NotePencil **New** action. The selected-Folder launcher does not repeat that New
action. **Open document** searches registered Co-work documents and their
Co-work-specific state. Create and import actions are disabled when the active
Folder does not grant the corresponding permission.

**New from Markdown** opens the operating system's native file picker, rooted
at the active Folder and filtered to `.md` and `.markdown`. The server accepts
only a real, contained, non-managed Markdown path after resolving its filesystem
identity; aliases cannot be used to enter `.wbuddy`. Co-work creates its
collaborative document representation from the existing bytes and continues to
use the original file; it does not copy, move, or rewrite the Markdown during
registration. If the file is already registered, Co-work opens that document.
If Markdown-file selection is unavailable on the host, the action is disabled
with a visible explanation rather than failing after a click.

For a newly created document, **Save in** defaults to the active Folder and
**Change** opens the native destination-Folder picker. The filename is derived
from the title and remains independently editable; the resulting relative path
still passes the same server-authoritative containment and reserved-name
validation as every document create. When destination-Folder selection is
unavailable, **Change** is disabled with a visible explanation while saving at
the Folder root remains available. A document started before a Folder is chosen
is simply untitled and **Saved on this device** until the user saves it into a
Folder. **Save document** is disabled with a visible explanation when the
dashboard is read-only, the active Folder denies create, or no Folder is active
and Folder selection is unavailable; the document remains safely on the device.
`scratch` is only an internal persistence term, not a separate user-facing
document type. Additional on-device documents are numbered
(`Untitled 2`, `Untitled 3`, and so on), and their human edits refresh an
**Edited** timestamp so recovery choices remain identifiable.

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
