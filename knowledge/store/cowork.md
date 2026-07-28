---
name: Co-work
kind: concept
description: The human-and-agent surface for living documents, organized around ordinary folders with durable editing, explicit file writes, and proposal review.
summary: A user opens an ordinary folder, Co-work inspects it without mutation, and a one-time confirmation discloses the .wbuddy support data before setup. An invariant toolbar owns New, New from Markdown, folder selection, document selection, and explicit folder closing. The launcher and document picker show only the active folder's registered documents while a folder is open, and only browser-local documents when no folder is open; every row states its location. Co-work restores the selected folder and document from the URL, keeps structured editing state durable through an offline-capable outbox, binds each document to one durable conversation with exact feedback anchors, writes Markdown only through an explicit human action, detects outside file changes before overwrite, and routes agent contributions through human-reviewed proposals.
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
  Native filesystem results are treated as untrusted input even though the picker runs on the host. Scoped Markdown and destination routes resolve the selected filesystem identity, derive the returned relative path from that resolved identity, and reapply containment plus the `.wbuddy` exclusion. This is load-bearing on Windows, where an 8.3 alias such as `WBUDDY~1` can otherwise disguise the managed directory. Registered Markdown identity is case-insensitive for Windows folder roots and case-sensitive for POSIX roots; the server remains authoritative for races and unusual Unicode identities.

  The Windows helper protocol is versioned and mode-bound. macOS cancellation returns an explicit protocol-and-mode-bound success marker; every nonzero `osascript` exit remains a genuine picker error. All picker modes share one non-blocking process lock.

  First-time setup is a focus-managed modal and disables stale document chrome from any previously active folder. If the short-lived inspection token expires after the user confirms setup, the provider refreshes inspection and retries initialization exactly once on that same click. Do not turn that bounded retry into a loop.

  Native folder, Markdown-file, and destination-folder picker availability are distinct server capabilities and must remain distinct through the client model. Permission and availability checks are repeated at intent dispatch boundaries, not left to disabled controls alone. In read-only mode, ordinary folder setup is informational and cannot initialize. A browser-local document remains local when the dashboard is read-only, its active folder denies create, or it has no folder and folder selection is unavailable.

  `wb.cowork.folder.close@1` is a dedicated navigation intent. It is not the folder-selection `cancel` action: cancel restores the context that existed before a transient picker or inspection, while **Close folder** deliberately clears the active folder and catalog. A registered session must pass the device-durability leave barrier first; an active browser-local document is folder-independent and remains open. Closing never unregisters a folder, retires a document, mutates `.wbuddy`, or changes Markdown.

  A document conversation ID is opaque and server-issued. GET binding inspection is read-only; only explicit Chat activation, feedback submission, or a routing decision may create the binding and ensure its driver. The driver receives durable user turns through a generation-scoped lease/cursor inbox and acknowledges each message only after handling it. Driver writes, questions, proposals, and comments carry the same generation fence so a stopped, restarted, or retired generation cannot mutate the document.

  `CoworkChatPanel` is a thin domain adapter over the canonical `widget-library/chat` `ConversationChat` surface. Co-work owns exact server-message-id-to-passage resolution, **Jump to passage**, routing notices, document-agent recovery mapping, document-scoped draft observation, and the surrounding editor/rail orchestration. Before a binding exists, the rail maps its document lifecycle into the shared `ChatPanelState`; it does not recreate the panel shell. Co-work must not fork canonical message rendering, question controls, composer behavior, loading/retry, or activity state. Co-work remains one cohesive durable Dashboard Core widget; the embedded reusable conversation surface is not a separately placeable widget instance.

  Document lifecycle operations span the folder's Truth/Ydoc databases and the house conversations database. They therefore acquire the cross-process per-store-and-document lifecycle lock before either side: start, feedback, and sitting routing hold it from active-state validation through their conversation effects, while retirement holds it through Truth commit and conversation close/lease revocation. Keep database work in the order lifecycle lock → Truth/Ydoc → conversations; never introduce the inverse nesting.

  Editor annotations are a runtime-only ProseMirror decoration projection derived from the same R2 document snapshot as the Review rail. They must never enter the schema, Yjs state, Markdown, undo history, or outbound persistence. Proposal and claim anchors are kind-qualified so identical raw IDs cannot collide. Review focus changes only the active treatment; rail filters never remove the underlying editor annotations. Chat passage highlighting is also view state and must preserve the editor selection and the user's current focus.

  Origin filtering is not persistence isolation: a later human Yjs update can causally depend on an earlier filtered struct. Never project a pending proposal into the live collaborative Y.Doc, even under a non-human origin. Sitting materialization starts from a clean clone of the canonical structured head, joins admitted decisions to the authoritative proposal catalog by ID and canonical hash, resolves every materializing anchor against that initial clone, rejects missing, mismatched, unresolved, duplicate, or overlapping edits, and applies confirmed changes in reverse document order. Explicit Save fails closed if tracked-suggestion schema artifacts somehow appear in the live document.

  Successful and response-recovery sitting paths do not adopt the prepared clone directly. They pull the authoritative committed state, verify its structured head, advance the current Markdown file hash, and then refresh the review projection. The canonical-state guard runs before preparation and after the server refresh. If a human edit advances the local generation while the sitting is in flight, the new file baseline is retained but the editor remains unsaved rather than falsely claiming to be current.
---

# Co-work

Co-work is work-buddy's surface for co-authoring living documents with an agent.
The unit a user chooses is simply a **folder**. Its displayed name is the
directory name; Co-work does not make the user define a second project object or
select a predefined document type. A completely ordinary folder can be inspected
without mutation. Choosing it is the user's intent to inspect and open it. If
the folder has not been used with Co-work before, the dashboard pauses before
mutation and asks the user to **Set up Co-work** there.

Setup creates `.wbuddy/manifest.yaml` for work-buddy-level metadata and the
canonical Co-work store at `.wbuddy/cowork/`. The confirmation names the
`.wbuddy` support data, shows the selected host path, and states that existing
documents are not changed. Cancelling it writes nothing. An already initialized
folder opens directly without asking again.

Because the dashboard has no authentication and Co-work can read host files,
all `/api/truth/cowork/*` and `/api/truth/doc/*` routes reject non-loopback
callers. Remote Co-work remains unavailable until Work Buddy has an
authenticated remote surface; a network-bound dashboard must not expose folder
contents by implication. The guard also requires a local browser-visible Host
and rejects forwarding/Tailscale proxy markers, because a loopback reverse
proxy is not itself proof of a local user. Opening host UI has an additional
boundary: every native folder, Markdown-file, and destination picker route
requires its exact Co-work intent header, rejects cross-site browser provenance
or a mismatched Origin, and the dashboard denies framing so another site cannot
place the controls in a clickjacking frame.

## Working with folders and documents

With no folder open, the toolbar's **folder** control opens the native picker
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

After read-only inspection, an ordinary folder shows the one-time setup
confirmation; an initialized folder opens its document catalog directly. The
confirmation is a focus-managed modal, begins on **Cancel**, and prevents
document actions from a previously active folder while the decision is open.
When the dashboard itself is read-only, the modal instead explains that Co-work
is not set up in the folder, offers only **Close**, and changes no files.

The document bar keeps the same foundational controls in every resting state:
the folder control, **Open document**, **New from Markdown**, and NotePencil
**New**. With no folder, New starts an ordinary browser-local document; with a
folder, it opens the contained create flow. New from Markdown remains in
the same toolbar position and is disabled with a specific explanation until an
import-capable folder and native Markdown picker are available. These creation
actions never appear in the launcher body or inside the Open document dialog.

The launcher has one **Documents** list whose contents follow the current
context. With a folder open, it contains only ready registered documents from
that folder; every row shows the folder name and relative Markdown path. With no
folder open, it contains only browser-local documents; every row shows italic
*Not saved to folder*, **Saved in this browser**, and its activity or recovery
detail. The whole row names and opens the document, with no repeated generic
Continue buttons and no competing “Recent documents” or “On this device”
sections.

The **Open document** picker follows the same boundary: active-folder documents
only while a folder is open, browser-local documents only otherwise. Search
never crosses that boundary. The folder-neutral launcher also has a separate
**Folders** section for known folder navigation. It is not called Recent folders
because registry order does not represent recency.

An active folder has a separate, visible **Close folder** control beside its
name. Closing a folder preserves the known-folder registry and every
browser-local document. It closes a registered document only after the existing
device-durability barrier succeeds, while an active browser-local document stays
open without folder context. A successful registered or idle close navigates to
`?mode=launcher`; a durability failure leaves the folder, document, catalog, and
URL in place.

**New from Markdown** opens the operating system's native file picker, rooted
at the active folder and filtered to `.md` and `.markdown`. The server accepts
only a real, contained, non-managed Markdown path after resolving its filesystem
identity; aliases cannot be used to enter `.wbuddy`. Co-work creates its
collaborative document representation from the existing bytes and continues to
use the original file; it does not copy, move, or rewrite the Markdown during
registration. If the file is already registered, Co-work opens that document.
If Markdown-file selection is unavailable on the host, the action is disabled
with a visible explanation rather than failing after a click.

For a newly created document, **Save in** defaults to the active folder and
**Change** opens the native destination-folder picker. The filename is derived
from the title and remains independently editable; the resulting relative path
still passes the same server-authoritative containment and reserved-name
validation as every document create. When destination-folder selection is
unavailable, **Change** is disabled with a visible explanation while saving at
the folder root remains available. A document started before a folder is chosen
is simply untitled, *Not saved to folder*, and **Saved in this browser**. Its
list metadata is stored in browser local storage and its structured content in
IndexedDB; it is not yet a Markdown file.

**Save document** can later place that browser-local document into an existing
or newly selected folder. Co-work first makes the local content durable, creates
and validates the registered document, opens that registered copy, and only then
retires the browser-local copy. A failure before the registered copy opens leaves
the browser-local document intact. Saving is disabled with a visible explanation
when the dashboard is read-only, the active folder denies create, or no folder
is active and folder selection is unavailable.

`scratch` is only an internal persistence term, not a separate user-facing
document type. Additional browser-local documents are numbered
(`Untitled 2`, `Untitled 3`, and so on), and their human edits refresh an
**Edited** timestamp so recovery choices remain identifiable.

The selected folder and document are encoded in the URL, so reload, history
navigation, and a shared local link restore the same working context. Document
selection and folder closing do not imply a file write. Retiring a document
removes it from the active catalog while preserving its Markdown file and
durable history.

## Editing and persistence

A Co-work document has two related representations:

- the authoritative structured collaborative head used by the editor, with
  monotonic versions and compare-and-swap protection; and
- the Markdown file in the selected folder, which is materialized only through
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

## Conversation and feedback

Every registered document has at most one durable conversation binding. The
binding ID is an opaque server-issued identifier; the dashboard never derives
one from the document or folder ID. Opening or reloading a document performs a
read-only binding lookup and does not start an agent. The first explicit Chat
action, selected-text feedback submission, redirect, or endorsement can create
the binding and ensure one document agent.

Selected-text feedback is saved verbatim as human-authored evidence, anchored to
the exact document passage, and posted as an ordinary user turn in that same
conversation. The response returns the real conversation and message IDs so the
chat can attach the anchor to the exact transcript message, including when two
feedback notes contain identical text. If agent startup fails after persistence,
the feedback remains visible and the user can explicitly restart Chat; the
dashboard does not claim that the authored feedback failed or silently retry it.

**Jump to passage** reveals the editor on a narrow screen, scrolls the anchored
quote into view, and briefly highlights it without replacing the editor's
selection or taking keyboard focus.

The document agent consumes a durable, ordered inbox and acknowledges a user
turn only after processing it. Restarting creates a new generation and fences
the old one from sending messages, asking questions, proposing edits, or adding
comments. Ordinary composer messages never answer a pending structured question
implicitly; a structured response names the exact question it answers.

Redirect and endorsement notices distinguish three outcomes: saved and sent to
a running agent, saved in Chat but awaiting restart, or not saved. A review
gesture can remain committed even if its follow-up chat delivery fails, so the
dashboard reports the conversation write and agent state returned by the server
rather than inferring success from the gesture itself.

Retiring a document closes its bound conversation and revokes the active driver
lease. Conversation start, feedback, routing, and retirement share one
cross-process document lifecycle boundary, preventing a late binding or agent
from appearing after retirement.

## Human and agent authority

The agent-facing capabilities are `cowork_doc_list`, `cowork_doc_get`,
`cowork_doc_propose_edit`, `cowork_doc_comment`, and `cowork_doc_expression_mark`.
An agent reads a document and proposes work on it. Every agent contribution is an
open proposal, never a decision. Accept, amend, reject, redirect, endorse, and
defer are human gestures collected on the dashboard, because an agent cannot
approve its own content.

A proposal with `replacement: ""` is an explicit tracked deletion of its exact
anchor. A flag keeps `replacement: null` and raises a concern without changing
text; agents must not use flags as a deletion workaround. Nonempty replacements
preserve their meaningful edge whitespace. Deletions cannot carry claim
references, because accepted deletion leaves no passage from which to mint an
expression.

The editor keeps every unresolved review annotation visible independently of
the active Review filter. These are view-only decorations, not hidden edits to
the document. Insertions and replacements show their proposed text beside the
anchored original; deletions show the original as translucent danger text with
a strikethrough. Flags, expressions or claims, and confirmed agent provenance
have distinct visual and non-colour treatments, and a flag remains a warning
underline rather than looking like removed text. Selecting a Review card or
moving through Queue scrolls to and strongly emphasizes only its
kind-qualified anchor. An explicit passage affordance also flashes that anchor.

The review rail groups proposals into a sitting so the user can decide them in
context. Accepting or amending a proposal applies only the admitted,
hash-matched proposal payload to an isolated clone of the canonical structured
document before that sitting is committed; unresolved review display never
mutates the live collaborative document. Explicit **Save** also refuses to
compact a live document containing tracked-suggestion artifacts. The document's
Markdown file is written through the materialization engine, never directly by
an agent, and the claims a document expresses live in the folder's scoped Truth
ledger through expression links. Internally, the engine still uses terms such
as scope root, store ID, and Truth store; the dashboard consistently calls the
thing the user selected a **folder**.
