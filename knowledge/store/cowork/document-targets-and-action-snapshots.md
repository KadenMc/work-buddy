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

The action bar supports:

- Working on;
- current selection;
- current section;
- custom range; and
- whole document.

**Work on this** promotes the current selection to the reusable target.
**Clear** returns Working on to the whole document.

The custom range uses keyboard-accessible **Set start here** and **Set end
here** actions at block boundaries. A draggable overview-ruler with two range
handles is not part of the current implementation; it can be added later
without changing the action-snapshot contract and must not become the only
input method.

## Target persistence and repair

The reusable target is presentation state stored in browser local storage
under the exact store/document identity. It is not embedded in collaborative
document content.

Its endpoints are encoded Yjs relative positions with quote, heading path, and
block hints for repair. Resolution prefers the relative range and can fall
back to the quote. An unresolved target blocks the action and asks the user to
choose a target again; it is never silently widened.

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

## Chat consumption

A targeted Chat message stores a safe action-snapshot reference in transcript
context. The generation-fenced document agent must call
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
