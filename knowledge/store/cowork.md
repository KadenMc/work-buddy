---
name: Co-work
kind: concept
description: The human-and-agent surface for living documents, with durable editing, source-safe file import, explicit file writes, content provenance, and proposal review.
summary: A user opens an ordinary folder, Co-work inspects it without mutation, and a one-time confirmation discloses the .wbuddy support data before setup. An invariant toolbar owns New, From file, folder selection, document selection, and explicit folder closing. From file uses a format-neutral importer boundary with Markdown support today; it creates a managed Co-work document without rewriting the source artifact. Co-work keeps structured editing state durable through an offline-capable outbox, records frozen-target authorship and human-review attestations for imported and pasted text, binds each document to one durable conversation with exact feedback anchors, and routes agent contributions through human-reviewed proposals.
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
  Native filesystem results are treated as untrusted input even though the picker runs on the host. Scoped import-file and destination routes resolve the selected filesystem identity, derive the returned relative path from that resolved identity, and reapply containment plus the `.wbuddy` exclusion. This is load-bearing on Windows, where an 8.3 alias such as `WBUDDY~1` can otherwise disguise the managed directory. Registered path identity is case-insensitive for Windows folder roots and case-sensitive for POSIX roots; the server remains authoritative for races and unusual Unicode identities.

  The Windows helper protocol is versioned and mode-bound. macOS cancellation returns an explicit protocol-and-mode-bound success marker; every nonzero `osascript` exit remains a genuine picker error. All picker modes share one non-blocking process lock.

  First-time setup is a focus-managed modal and disables stale document chrome from any previously active folder. If the short-lived inspection token expires after the user confirms setup, the provider refreshes inspection and retries initialization exactly once on that same click. Do not turn that bounded retry into a loop.

  Native folder, supported-import-file, and destination-folder picker availability are distinct server capabilities and must remain distinct through the client model. `chooser.import_available` is the canonical transport field and `folderChooser.importAvailable` is the canonical client property; `markdown_available` may still be emitted or accepted as a legacy compatibility alias. Permission and availability checks are repeated at intent dispatch boundaries, not left to disabled controls alone. In read-only mode, ordinary folder setup is informational and cannot initialize. A browser-local document remains local when the dashboard is read-only, its active folder denies create, or it has no folder and folder selection is unavailable.

  `wb.cowork.folder.close@1` is a dedicated navigation intent. It is not the folder-selection `cancel` action: cancel restores the context that existed before a transient picker or inspection, while **Close folder** deliberately clears the active folder and catalog. A registered session must pass the device-durability leave barrier first; an active browser-local document is folder-independent and remains open. Closing never unregisters a folder, retires a document, mutates `.wbuddy`, or changes Markdown.

  A document conversation ID is opaque and server-issued. GET binding inspection is read-only. Preparing Chat may create the canonical binding and pin the displayed execution selection, but it must not start a model. Persisting an authored Chat, feedback, routing, or Co-think discussion turn automatically wakes or starts the driver. The driver receives durable user turns through a generation-scoped lease/cursor inbox and acknowledges each message only after handling it. Driver writes, questions, proposals, and comments carry the same generation fence so a stopped, restarted, or retired generation cannot mutate the document.

  The shared Chat surface assigns one caller-stable message ID before Co-work
  freezes target context. An uncertain delivery retries the exact prepared
  content, context, and ID. Identical replay returns the original durable turn;
  conflicting reuse is rejected. Distinct authored sends remain distinct even
  when their text happens to match.

  The provider/model selection is durable conversation authority, not browser settings. Non-executing Chat binding pins the validated server default returned to the picker, so the first authored turn cannot run on an undisplayed target. A live change is one revision-checked atomic pair and restarts only the document driver; transcript, draft, binding, and document state remain. Copy producer attribution from the exact active lease, never from request fields or the conversation's current selection.

  On a changed selection, raw lease status is the fencing authority: any persisted `starting` or `running` lease must be stopped before the new selection is launched even if a stale heartbeat or failed liveness probe projects it as stopped. The write guard accepts raw active leases, so using only presentation status would leave the old generation authorized.

  `CoworkChatPanel` is a thin domain adapter over the canonical `widget-library/chat` `ConversationChat` surface. Co-work owns exact server-message-id-to-passage resolution, **Jump to passage**, routing notices, document-agent recovery mapping, document-scoped draft observation, and the surrounding editor/rail orchestration. Before a binding exists, the rail maps its document lifecycle into the shared `ChatPanelState`; it does not recreate the panel shell. Co-work must not fork canonical message rendering, question controls, composer behavior, loading/retry, or activity state. Co-work remains one cohesive durable Dashboard Core widget; the embedded reusable conversation surface is not a separately placeable widget instance.

  Document lifecycle operations span the folder's Truth/Ydoc databases and the house conversations database. They therefore acquire the cross-process per-store-and-document lifecycle lock before either side: start, feedback, and sitting routing hold it from active-state validation through their conversation effects, while retirement holds it through Truth commit and conversation close/lease revocation. Keep database work in the order lifecycle lock → Truth/Ydoc → conversations; never introduce the inverse nesting.

  Editor annotations are a runtime-only ProseMirror decoration projection derived from the same R2 document snapshot as the Review rail. They must never enter the schema, Yjs state, Markdown, undo history, or outbound persistence. Proposal and claim anchors are kind-qualified so identical raw IDs cannot collide. Review focus changes only the active treatment; rail filters never remove the underlying editor annotations. Chat passage highlighting is also view state and must preserve the editor selection and the user's current focus.

  Review Stream cards stay in normal document flow inside one ordinary Review scroll container. Do not position or transform cards from editor-anchor geometry, subscribe card layout to editor transactions, or compensate for rail scrolling; those cross-pane geometry loops previously produced blank space and snap-back behavior. The editor and Review remain independent sibling scroll owners. Passive selection reconciliation—initial render, filtering, mode changes, and data/decorations refresh—may restore the editor's focused treatment but must not move either surface. A present-user activation of a card, Queue target, or recovery link is a one-shot command that selects and reveals the corresponding editor passage; recovery links first expose their target in Stream/All, and the explicit passage affordance also flashes it. On a narrow workspace, the surface exposes the Editor before revealing. Never persist or replay a reveal command during anchor/decorations refresh: only the current kind-qualified focused identity may be reapplied during the mounted session. A future contextual margin view would need to share the editor's scroll plane rather than recreate independent editor-to-rail alignment.

  Scroll persistence is a device-local callback-ref binding keyed by full folder ID, document ID, and surface (with an explicit document-only namespace for browser-local and demo documents). Attach the Review binding only while Review is visible and only for **Stream** + **All**. Detach it synchronously before switching to Chat, a filter, or Queue, so shorter replacement content cannot clamp the canonical position before it is saved. Writes are throttled and flushed on unmount, page hide, and document visibility loss. A saved position may exceed a loading shell's current range, so restoration observes later geometry for a bounded period and must never persist that temporary clamp. Wheel or touch movement, scroll keys directed at the container itself, an external programmatic scroll, or explicit passage navigation cancels pending restoration and becomes the new position. Ordinary clicks, caret placement, and descendant control keys do not cancel it before scrolling actually occurs.

  Origin filtering is not persistence isolation: a later human Yjs update can causally depend on an earlier filtered struct. Never project a pending proposal into the live collaborative Y.Doc, even under a non-human origin. Sitting materialization starts from a clean clone of the canonical structured head, joins admitted decisions to the authoritative proposal catalog by ID and canonical hash, resolves every materializing anchor against that initial clone, rejects missing, mismatched, unresolved, duplicate, or overlapping edits, and applies confirmed changes in reverse document order. Explicit Save fails closed if tracked-suggestion schema artifacts somehow appear in the live document.

  Batch preparation is a non-mutating preflight, not an all-purpose error boundary. Canonical Markdown selectors and visible ProseMirror text use different whitespace representations, so client context matching must tolerate Markdown line and block boundaries while still resolving exactly one occurrence. If any selected decision is unavailable, changed, unresolved, or conflicts with another selected edit, cancel the prepared intent and return item-level blockers plus the independently applicable subset. Review keeps every decision selected and requires a second explicit action before submitting that subset; it never silently converts the user's batch into a partial application. A deterministic cancellation retires that attempt's idempotency key before any same-selection retry; an uncertain commit/response failure retains the key so retry can recover the receipt. Subset retries remain bound to the exact explicitly confirmed decisions and retained blockers. The user may explicitly remove a blocker; because overlap diagnoses depend on the whole selection, doing so clears the old diagnosis and requires a fresh preflight of the remaining decisions.

  Successful and response-recovery sitting paths do not adopt the prepared clone directly. They pull the authoritative committed state, verify its structured head, advance the managed projection, and then refresh the review projection. A document with `source.writeback_policy=never` commits that projection internally and never publishes it over the import source artifact. The canonical-state guard runs before preparation and after the server refresh. If a human edit advances the local generation while the sitting is in flight, the new baseline is retained but the editor remains unsaved rather than falsely claiming to be current.

  Before sitting admission, the editor publishes the exact current Y.Doc
  snapshot and canonical Markdown together, with bounded recapture if an edit
  races the checkpoint. The server persists the verified projection blob before
  its receipt and may pass an internal prevalidated applicability proof only
  while the sitting's expected-head and snapshot checks still hold.

  Paste persistence and paste provenance are related but cannot be committed in one browser transaction: Yjs state, the synchronous local-storage intent journal, the document-scoped IndexedDB provenance outbox, and the server Truth store are separate authorities. The journal is the smallest recovery barrier across that gap, not an atomicity claim. Never delete a frozen provenance request before a confirmed server receipt, and never retarget one implicitly after an absent, ambiguous, or changed target. An actor-binding rejection is the exception that requires explicit recovery: refetch the current actor, invalidate every stale frozen request, rotate its idempotency key, reset its determination to unknown, and require a fresh user attestation before sending.
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
boundary: every native folder, import-file, and destination picker route
requires its exact Co-work intent header, rejects cross-site browser provenance
or a mismatched Origin, and the dashboard denies framing so another site cannot
place the controls in a clickjacking frame.

## Working with folders and documents

With no folder open, the toolbar's **folder** control opens the native picker
directly. On Windows the dashboard launches a fixed
`python -I -m work_buddy.cowork.folder_picker_helper` command with only bounded,
validated mode and starting-directory arguments. That isolated PySide6 process
asks Qt for the operating system's native directory or supported-file dialog and
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
the folder control, **Open document**, **From file**, and NotePencil **New**.
With no folder, New starts an ordinary browser-local document; with a folder, it
opens the contained create flow. From file remains in the same toolbar position.
With no folder, it starts folder selection and retains the import intent; after
the chosen folder is opened or set up, Co-work continues directly into the file
import. It is disabled only when read-only mode, folder permissions, or host
capabilities make that continuation unavailable. These creation actions never
appear in the launcher body or inside the Open document dialog, and catalog
loading does not keep them locked after a writable folder has been established.

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

**From file** opens the operating system's native file picker, rooted at the
active folder. Its outer contract is format-neutral and returns a versioned
importer identity and media type. The only importer today is `markdown/v1`,
which accepts UTF-8 `.md` and `.markdown` files as `text/markdown`, up to its
16 MiB source limit. Each importer owns path acceptance, media type, title
derivation, source limits, and conversion; adding a later Word importer does not
change the outer picker, bootstrap, or provenance flow. The server accepts only
a real, contained, non-managed path after resolving its filesystem identity;
aliases cannot be used to enter `.wbuddy`. Its validated importer descriptor is
authoritative: the browser chooses a converter by the exact versioned importer
ID and stops before commit if that version is unavailable, rather than inferring
a converter from a suffix or caller-supplied media type.

Subsequent observation of a detached source is governed by that persisted
importer descriptor too. Co-work reads only a regular file, does not follow
links or reparse points, and enforces the importer's source-size limit while
hashing. Routine document lists, reads, and drift checks report the observed
source digest as unknown when the source is unsafe, unavailable, changed during
the read, or oversized; the managed Co-work document remains usable. An
explicit request for the current external source returns a typed failure instead
of hiding the reason.

The Markdown importer performs supported import normalization into the
structured editor model. Formatting details such as hard line wrapping may
therefore differ in the managed projection even though the source artifact's
meaning is retained. Co-work records the exact source artifact hash separately
from the managed projection hash and retains the exact selected bytes in its
content-addressed store. Portable Truth export carries those bytes when they
were captured; historical import records may remain hash-only. The imported
document has `source.writeback_policy=never`: editing, accepting a proposal,
Ctrl+S, retirement, or recovery never copies, moves, or rewrites the original
file.

If that path is already registered as a detached import and the newly selected
bytes match the recorded import hash, Co-work opens the existing managed
document. If the file changed, or a historical record has no comparable import
hash, Co-work warns and offers to open the existing Co-work copy without
refreshing it from the file. It never silently adopts the changed file as a
replacement version. If file selection is unavailable on the host, **From
file** is disabled with a visible explanation rather than failing after a
click. The canonical availability property is `importAvailable`;
`markdown_available` remains a legacy compatibility alias.

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
removes it from the active catalog while preserving any source artifact,
writeback file, managed projections, and durable history.
After a route resolves, the dashboard uses the first eight characters of
`store_id` only when that prefix uniquely identifies one entry in the
authoritative Folder catalog, and the first eight characters of `document_id`
only when that prefix uniquely identifies one document inside the resolved
folder. Exact full IDs remain valid, and a collision keeps the corresponding
full ID. Both prefixes are presentation-only: provider state, durability keys,
and every API request use the full permanent identities.
For a detached import, removal first retries and flushes pending edits, validates
the canonical head, and compacts the Yjs tail into a durable internal snapshot.
It may therefore retain a newer structured head than its latest managed
projection, but it never invokes the external Save that the document is
intentionally forbidden to perform.
The retired identity permanently reserves its original path. Selecting that
exact source through **From file** offers **Choose another file** rather than
opening or replacing retired history; copying or renaming the source produces a
distinct path that can be imported as a new document.

## Editing and persistence

A Co-work document has two canonical representations:

- the authoritative structured collaborative head used by the editor, with
  monotonic versions and compare-and-swap protection; and
- a versioned managed Markdown projection rendered from that structured head.

A document created in Co-work may use its folder-relative Markdown path as an
explicit Save target. A document created through **From file** instead keeps the
selected file only as a source artifact and never writes back to it. The source
artifact hash, exact retained source bytes, managed projection hash, and
structured-head hash remain separate facts. If source and projection bytes are
identical they can share one content-addressed blob without collapsing those
roles.

Local edits enter an IndexedDB outbox before transport. The provider can reload
them after a browser refresh or temporary disconnection and acknowledges them
only after the server durably accepts them. Opening or reimporting a document
uses a durability barrier and one atomic model commit, so a delayed request
cannot replace a newer navigation choice.

Editor and Review scroll positions are device-local interface continuity, not
document state. Co-work stores them separately per folder, document, and
surface, then restores them when that document is reopened. Restoration waits
for late editor hydration or Review loading, but any user scroll intent or
explicit passage navigation wins immediately. Only the canonical **Stream** +
**All** Review view owns the saved Review position; Queue, filtered views, and a
hidden Review tab cannot replace it with their temporary geometry.

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

Before writing to a configured Save target, Co-work compares the registered file
fingerprint with the current file. An outside change blocks overwrite and offers
an explicit reimport path. These drift and reimport rules do not treat a
non-writeback import source as a live projection or Save target. Reimport
prepares the catalog and structured head off-model, then replaces the visible
document atomically; failure leaves the current editor intact. An ambiguous
commit response is retried with the exact retained idempotent payload rather than
rereading staged source that may already have been consumed. If another tab
retires the active document, catalog reconciliation first makes local edits
device-durable and then revokes the writable session. Recovery and quarantine
paths fail closed when persisted state cannot be validated.

## Content provenance

Co-work records authorship and human review as separate, append-only
attestations. **From file** uses a provenance determination before commit and
binds it to the exact imported document version. A text-bearing paste binds the
same dimensions to an exact quote-anchored span and one structured-head digest.
Authorship can be human, AI, mixed, or unknown. For AI or mixed authorship,
human review records whether a person reviewed the content and can identify
that reviewer.

A paste opens the shared provenance form when it contains multiple top-level
blocks, a list, task list, code block, blockquote, or table, or at least 600
Unicode characters in one ordinary block. A shorter, single ordinary block is
automatically attributed to the current local user and explicitly marked with
the `automatic_short_text_attribution` basis. That is a low-friction heuristic,
not proof of clipboard authorship. This slice records text that the editor
actually inserted; it does not attest image-only or attachment-only clipboard
content or preserve original clipboard HTML.

A paste over 1,000,000 Unicode characters is stopped before entering Yjs or the
provenance delivery stores and asks the user to paste in smaller sections. This
keeps every accepted edit within the exact-span limit the server can record.

Pending paste attribution uses a document-scoped IndexedDB FIFO outbox
plus a synchronous local-storage intent journal. After editor hydration, Co-work
reconciles the journal, requires the full quote anchor to identify one current
passage, flushes the Yjs edit, freezes the complete request against that exact
structured head, and retries it unchanged until receipt. Yjs and the provenance
record are not one atomic transaction; the journal makes the cross-store gap
recoverable. A changed, absent, or ambiguous target remains visible for explicit
recovery rather than being attached elsewhere.

The attestation says what the acting person reports about the content. It does
not prove authorship, verify a claim, certify correctness, or approve the text.
A server-derived actor binding identifies **Me**. Co-work freezes its ref and
identity status at capture and revalidates both on delivery, so an account
switch cannot silently reassign a pending import or paste. The current local
dashboard uses `identity_status=local_actor_ref` but has no authenticated
multi-user boundary. A typed other-person name remains
`identity_status=claimed_name`, not an account identity. The schema reserves
`identity_status=account_ref` for a future authenticated participant directory;
the current dashboard does not mint it. If a queued paste reaches the server
after the actor binding changes, Co-work refetches the actor and requires a
fresh explicit determination for every pending entry rather than silently
rebinding or repeatedly resending stale attribution. See
`cowork/content-provenance`.

## Conversation and feedback

Every registered document has at most one durable conversation binding. The
binding ID is an opaque server-issued identifier; the dashboard never derives
one from the document or folder ID. Opening or reloading a document performs a
read-only binding lookup and does not start an agent. Preparing the Chat pane can
create or reuse that binding and pin its displayed model selection, but still
does not run a model. Persisting an authored Chat message, selected-text
feedback, redirect, endorsement, or Co-think discussion automatically wakes or
starts one document agent.

The Chat header's optional **Run with** picker selects one provider/model pair
for that durable conversation. Claude Code uses the user's signed-in Claude
account; Codex uses the user's ChatGPT account through the official Codex
runtime. Provider and model availability is server-discovered. The picker does
not imply API billing, store a browser-only preference, or silently fall back to
another provider.

Before the conversation exists, the picker shows the server default without
creating anything. Chat preparation pins the validated pair returned to the
picker without starting the assistant driver. Changing an active conversation
asks for confirmation, then restarts only that driver. The messages and unsent
draft stay. The selection is revisioned so two open surfaces cannot silently
overwrite each other, and assistant messages retain the provider/model that
actually produced them. Read-only documents may inspect the saved selection but
cannot change it or send a turn that starts a new driver.

Selected-text feedback is saved verbatim as human-authored evidence, anchored to
the exact document passage, and posted as an ordinary user turn in that same
conversation. The response returns the real conversation and message IDs so the
chat can attach the anchor to the exact transcript message, including when two
feedback notes contain identical text. If the automatic wake fails after
persistence, the authored turn and feedback remain visible, Chat reaches **No
response received.**, and ordinary composition becomes available again. A later
authored turn makes another bounded wake attempt; there is no user-facing
Restart lifecycle control.

**Jump to passage** reveals the editor on a narrow screen, scrolls the anchored
quote into view, and briefly highlights it without replacing the editor's
selection or taking keyboard focus.

The document agent consumes a durable, ordered inbox and acknowledges a user
turn only after processing it. Restarting creates a new generation and fences
the old one from sending messages, asking questions, proposing edits, or adding
comments. Ordinary composer messages never answer a pending structured question
implicitly; a structured response names the exact question it answers.

Each authored Chat turn receives one caller-stable message ID before Co-work
freezes its target context. If delivery acknowledgement is uncertain, the exact
prepared content, context, and ID are retried together. An identical replay
resolves to the existing durable turn rather than creating another inbox item.
This delivery idempotency complements the generation-scoped lease and cursor;
it does not collapse distinct user sends merely because their text matches.

The driver runs outside the selected folder and receives document authority only
through a generation-scoped, server-enforced MCP session. Neither provider gets
the folder path or project instructions through its working directory, command
arguments, or prompt. See `architecture/agent-execution` for provider isolation,
catalog, persistence, and process-ownership details.

Redirect and endorsement notices distinguish three outcomes: saved and sent to
a running agent, saved in Chat but not answered, or not saved. A review
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
underline rather than looking like removed text. The Review **Stream** is a
conventional normal-flow list in document order, with filters acting as lenses
over that list and **Queue** providing sequential focus. An unselected Stream
card keeps only its scan-level identity and title visible; selection discloses
its quote, rationale, evidence, and item-specific controls. The whole
non-control card surface is an efficient pointer target, while the title remains
the semantic keyboard button and embedded passage or inspector controls keep
their own actions. Activating a Review card or moving through Queue selects its
kind-qualified target and reveals that passage in the editor without moving
Review itself. The explicit passage affordance performs the same reveal and
briefly flashes the anchor. Merely reconciling an already-selected item or
remounting the editor projection restores its emphasis without scrolling;
current selection is rail state, while navigation intent is a one-shot command.

Queue keyboard commands come from the registry-backed Co-work settings page.
The same atomic shortcut map owns previous, next, positive decision, amend,
negative decision, and defer bindings, so the settings UI can reject conflicts
before saving and the rendered key hints cannot drift from runtime behavior.
Shortcuts operate only while Review's Queue is visibly active, never steal text
entry or composition, and dispatch through the same applicability and staging
paths as their corresponding buttons. The Co-work view links directly to its
own App settings page; the reusable keybinding-map control is host Settings UI,
not a Review-only configuration surface.

The review rail groups proposals into a sitting so the user can decide them in
context. Accepting or amending a proposal applies only the admitted,
hash-matched proposal payload to an isolated clone of the canonical structured
document before that sitting is committed; unresolved review display never
mutates the live collaborative document. Explicit **Save** also refuses to
compact a live document containing tracked-suggestion artifacts. The managed
projection is committed through the materialization engine, never directly by
an agent; only a document with a writeback target may publish that projection to
a file. The claims a document expresses live in the folder's scoped Truth ledger
through expression links. Internally, the engine still uses terms such as scope
root, store ID, and Truth store; the dashboard consistently calls the thing the
user selected a **folder**.

Before a multi-decision sitting changes the document, Co-work confirms that the
selected edits can be placed together against one synchronized document head. A
blocked decision does not collapse the batch into a generic failure and does not
cause the other decisions to be applied silently. Review identifies the blocked
suggestions, keeps all choices selected, and—when an independent subset is ready—
offers an explicit action to apply only those other decisions.

Proposal base hashes remain immutable drafting lineage; they are not a
whole-document applicability veto. Review accepts a matching structured head
as current and otherwise requires the immutable quote selector to resolve
uniquely against a receipt-bound current canonical Markdown projection. An
unrelated edit may therefore move a proposal without invalidating it. A
missing, ambiguous, or unverifiable passage blocks only **Accept** and
**Amend**. **Reject**, **Dismiss**, **Defer**, **Redirect**, and **Endorse**
remain proposal-level decisions because they do not apply text.
