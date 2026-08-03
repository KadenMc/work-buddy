---
name: Co-work document targets and action snapshots
kind: system
description: Reusable Working on ranges and immutable exact-version action targets shared by Chat, Verify, and Co-think.
summary: A live selection, device-local document target, and durable action snapshot are separate states shared by Chat, Verify, and Co-think. The browser freezes Yjs state, canonical Markdown, target selector/text/reference, and structured-head hashes; the server validates and persists the immutable action before use.
entry_points:
- dashboard-react/src/apps/cowork/targets
- work_buddy.cowork.verify.service
- work_buddy.cowork.chat_targets
tags:
- cowork
- document-target
- action-snapshot
- Working-on
- yjs
- exact-version
- chat
aliases:
- Working on
- document focus
- action target
- exact document context
- review scope
parents:
- cowork/verify-and-cothink
dev_notes: |-
  Python treats Yjs as opaque. `DefaultCoworkActionSnapshotController` owns the
  capture race because the browser owns the live editor and canonical Markdown
  serializer. The server remains authoritative for document identity, durable
  structured head, content hashes, target containment, allowed-change ranges,
  and egress boundaries.
---

## Three different states

**Selection** is the text highlighted in the editor now. It is ephemeral.

**Document target** is the reusable **Working on** default for one registered
document on one device. It guides later actions but does not itself authorize
reading, changing, or sharing content.

**Action target** is the exact immutable target captured for one Chat message,
Verify run, Co-think invocation, or other action. Later selection or document
changes cannot rewrite it.

Use **document target** or **Working on**, not bare **scope**. “Scope” is too
ambiguous with folder/project scope, readable context, change range, and data
sharing.

## Available choices

The compact editor-top controls manage only the reusable Working on target:

- **Set by selection** promotes the current selection with exact character
  endpoints;
- **Set by cursor** records an exact start cursor and then an exact end cursor;
  and
- **Clear** returns Working on to the whole document.

Ordinary Verify runs and authored Chat messages use Working on directly; the
Verify dock does not add a second target picker. A proposal-linked recheck may
rebind its original exact durable target without rewriting Working on. Only a
legacy unresolved recheck asks the person to set and separately affirm a new
exact Working on passage.

The active Working on range is view-only editor state: its text is highlighted
and its exact start/end boundaries have visible markers. These decorations do
not enter Yjs, Markdown, undo history, or outbound persistence. A draggable
overview ruler with two range handles remains optional; it must not replace the
keyboard-accessible selection and cursor workflows.

## Target persistence and repair

The reusable target is presentation state stored in browser local storage
under the exact store/document identity. It is not embedded in collaborative
document content.

Its exact endpoints are encoded Yjs relative positions with quote, heading
path, and block hints for repair. Resolution prefers the relative range and can
fall back to the quote. Character-granularity references remain character
granular during repair; older references without granularity retain their
block behavior. An unresolved target blocks the action and asks the user to
choose a target again; it is never silently widened.

A character range may legitimately begin at the first ProseMirror text
position. The Yjs adapter also guards that position because a stale
item-associated reference can appear there after a collaborative block
reorder. Co-work accepts the current-coordinate translation only for a
character target whose stored start and end block identities both match the
resolved range. Missing or mismatched structural proof falls through to quote
repair or fails closed.

## Stable capture protocol

`DefaultCoworkActionSnapshotController.capture()`:

1. records the editor’s current edit generation;
2. retries and flushes pending Yjs persistence;
3. compacts the exact durable state and receives its structured-head receipt;
4. synchronously resolves the chosen target and serializes canonical Markdown;
5. captures the Yjs update and state vector;
6. hashes the snapshot, state vector, projection, and target text;
7. verifies that the edit generation and durable hashes did not change; and
8. retries the whole boundary when they did.

The resulting browser object contains a fresh capture ID, exact store/document
identity, Yjs generation and snapshot hashes, structured head, canonical
projection bytes/hash, target selector/text hash, and target source/label.

The server validates the current durable head, recomputes content hashes,
resolves the selector, writes the projection/target blobs, and records an
immutable `ActionSnapshot` with separate:

- context boundary;
- allowed change ranges; and
- egress boundary.

Compaction intentionally consumes the structured update tail before the
action-snapshot request. For a capture compaction, the request frames the exact
Y.Doc snapshot and UTF-8 Markdown projection together. The server re-hashes
both, commits the snapshot under the structured-head CAS, and publishes one
operational projection receipt through the same crash-recovery boundary. The
later action snapshot must match that receipt's document, Y.Doc generation,
snapshot, structured head, and projection digest. A stale CAS makes the browser
regenerate both projection and snapshot; it never retries a refreshed Y.Doc
with old Markdown. A later structured update or ordinary compaction invalidates
the receipt.

This receipt proves that the opaque snapshot and projection were admitted
together. Python still does not interpret Yjs, so it does not independently
derive Markdown from the snapshot; that would require a server-side Yjs runtime
and the canonical serializer. The browser remains the trusted serializer while
the server owns concurrency, durable binding, target containment, and egress.

## Chat consumption

An ordinary authored Chat message inherits Working on and stores a safe
action-snapshot reference in transcript context. Its compact **About:** chip
reports that shared target; there is no independent sticky toggle. Structured
answers to an existing agent question remain bound to that question rather
than capturing a second target.

If a collaborator advances the structured head between capture and request,
Chat may recapture once, but it pins that retry to the first capture's durable
target reference. A concurrent Working on change cannot silently retarget the
message. Any second conflict or unresolvable original target stays visible and
retains the draft.

The generation-fenced document agent must call
`cowork_action_snapshot_get` with the exact message and snapshot IDs. A
terminal fetch records a generation-bound consumption receipt; the reply and
acknowledgement must echo that receipt.

This distinguishes “the message mentioned a target” from “the responding agent
actually consumed that frozen target.”

When the frozen view is present and valid, the receipt has
`fetch_outcome=available`. When it is missing or fails integrity validation,
the capability still returns a durable receipt with
`fetch_outcome=unavailable`. That receipt authorizes only a truthful
receipt-bound reply and acknowledgement explaining that the exact context
could not be opened; it cannot support a document proposal or comment. The
durable turn therefore does not deadlock on corrupt or missing context.

A restart creates a new receipt for the new consumer generation. If an earlier
generation already committed the stable reply but crashed before
acknowledgement, the new generation may reuse that first reply only after the
store proves that both receipts bind the same conversation, user message,
action snapshot, and transcript-visible target/Co-think context. The new
receipt is then bound to the stable reply and may acknowledge the turn, while
both generation receipts remain auditable. Changed target semantics or reuse
of the reply ID for another turn is rejected.
