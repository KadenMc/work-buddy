---
name: Co-work Truth surface
kind: system
description: First-class Co-work rail surface for observing and managing the claims, expressions, provenance, and lifecycle beneath a document or Folder.
summary: Truth is a peer of Review and Chat. Review is the modification inbox; Truth supports both observability and contextual modification. The Truth tab defaults to claims expressed by This document, can widen to Folder, and offers a Facts filter for current authoritative claims. Its editor lens displays expression and provenance decorations without changing or scrolling the document.
entry_points:
- work_buddy.truth.queries
- work_buddy.truth.expressions
- work_buddy.truth.review
- work_buddy.cowork.api
- work_buddy.cowork.truth_api
- work_buddy.cowork.truth_surface
- dashboard-react/src/apps/cowork/rail/CoworkRail.tsx
- dashboard-react/src/apps/cowork/editor/ledgerDecorations.ts
tags:
- cowork
- truth
- claims
- expressions
- facts
- provenance
- editor-lens
- review-rail
aliases:
- Truth tab
- document truth
- claim inspector
- Truth rail
parents:
- cowork
- truth
dev_notes: |-
  Truth owns a separate read projection/provider; do not enlarge ReviewRailData
  into a general ledger transport. Preserve full lifecycle states and keep
  base_status separate from the needs_review overlay. SSE is only an
  invalidation nudge followed by an authoritative repull.

  A Truth lens is a ProseMirror decoration state, never document state. Lens
  changes must not write a transaction that alters the document, Y.Doc,
  Markdown, editor selection, scroll position, or undo history. A user passage
  activation is a one-shot reveal command and is never persisted or replayed.
  For an expression in another document, carry its exact quote selector only
  through the requested document switch, reveal it after that editor is ready,
  and clear the request on either success or failed navigation.

  There is no append-only expression unlink/retraction operation. Do not add a
  Remove connection control or delete expression rows. Challenge and supersede
  need their canonical relation/reason flows rather than a generic
  status-button row. Redaction is available only through its dedicated,
  hash-bound warning and confirmation flow.
---

# Co-work Truth surface

Truth is a first-class user surface within Co-work. The rail tabs are
**Review | Truth | Chat**:

- **Review** answers “What needs my decision?” and remains the inbox for edit
  proposals, flags, evaluation results, and actionable claim states.
- **Truth** answers “What does this work rest on?” and supports both observation
  and contextual management of the ledger.
- **Chat** remains the conversational interaction surface.

The product-visible **Truth** name is deliberate. Internal implementation terms
such as `TruthStore`, hashes, raw IDs, gestures, and rows remain out of routine
copy.

## Terms and boundaries

A **claim** is a proposition in the append-only ledger. **Facts** is a filter,
not a claim type or a second subsystem. It uses the canonical current-claim
semantics: confirmed base status, current validity, not redacted or voided, and
no active needs-review overlay. Do not implement it as
`claim_kind == "fact"`.

An **expression** is a passage where the document says a claim. **Evidence** is
why the claim is believed. A selected or matching passage does not become
evidence automatically. The needs-review overlay is also separate from the
claim's base lifecycle status.

Truth defaults to **This document**, which contains claims with expression rows
in the open document. **Folder** widens the view to the selected Co-work
Folder's complete claim ledger, including claims expressed elsewhere and
unconnected claims. Routine UI says Folder, not bare scope.

Available state filters are **All claims**, **Facts**, **Proposed**, **Needs
review**, **Challenged**, and **Unconnected**. The detail view shows the exact
proposition, base and overlay state, claim kind, validity, creator, all
expressions and roles, receipts/provenance, and lifecycle history. Each
expression has its own exact passage-navigation action. Activating an
expression in another document opens that document by its full identity and
reveals the selected expression exactly once; later refreshes may restore
emphasis but must not replay the scroll.

## Review cross-listing

Only claims requiring attention are cross-listed into Review: proposed,
challenged, and active needs-review claims. Ordinary confirmed facts and
terminal history remain observable in Truth without filling the modification
inbox. Both projections use the same permanent claim ID, so a legitimate
decision refreshes both surfaces.

Cross-listed Truth attention belongs inside Review's existing scroll body. An
unbounded claim list must never sit above that body and compress the proposal
workspace.

Truth does not host edit proposals, Verify setup, Co-think controls, or a second
conversation. Review and Chat remain the interaction surfaces for those jobs.

## Editor lenses

The editor lens is explicit view state:

- `review` projects proposals, flags, and evaluation-result anchors;
- `truth` projects expression and provenance anchors; and
- `neutral` projects neither persistent ledger overlay.

Review selects the review lens, Truth selects the truth lens, and Chat selects
neutral. Temporary Working on and Chat passage highlights remain independent.
A lens switch replaces decorations only and clears incompatible focus; it never
changes content, selection, scroll, or persisted document state. Clicking a
claim or one of its expressions issues one present-user navigation command.
Refresh may restore emphasis but never replay that reveal.

## Modification authority and limits

Truth's primary creation actions are **Propose from selection** and **Connect selection**.
Connecting records where a claim is expressed without editing prose or
inventing support. Creating from selected prose may produce a proposed claim
plus an expression, but not a confirmed fact.

Mutations call canonical Truth/Co-work operations. Agents may propose and
humans retain confirmation authority over exact content and displayed context.
That decision context includes lifecycle and review state, support and premise
assessment, conflicts and derivations, source-integrity state, and prose
connections—not only the claim text and receipt rows. Any drift requires a
fresh review.
Read-only mode still permits browsing, history inspection, and passage
navigation while mutation controls explain why they are unavailable.
Confirm and Reaffirm are validated against the exact lifecycle state in the
same write transaction. Quarantined-only support remains observable, but this
surface suppresses confirmation until a dedicated quarantine-override ceremony
is designed.

Do not expose an unconditional six-verb claim bar. Challenge requires a
supported conflicting claim; supersede requires a successor, relationship, and
reason. The surface reserves those operations for their dedicated flows.
Redaction is the narrow exception: it is destructive, so it appears only behind a
dedicated warning, reason, and exact hash-bound confirmation. Expressions
currently have no safe append-only unlink or retraction lifecycle, so the
surface must not offer Remove connection.

## UX invariants

Persist tab, This document/Folder selection, filter, and independent Truth
scroll per full Folder and document identity. Restoration never replays passage
navigation. Background refresh retains prior data and geometry; first-load
failure offers Retry and is not rendered as an empty ledger.

List scroll is independent from transient claim details and selection forms.
Entering a drill-in starts it at the top without overwriting the saved list
position; returning restores the list. Scope and filter changes deliberately
reset the new list to the top. Paginated backend reads are exhausted by the
provider so Folder claims, connection candidates, and Review cross-listing are
never silently capped at the first page.

Distinguish no document connections, an empty Folder ledger, and no filter
matches. The rail tablist uses roving keyboard focus with Arrow keys and
Home/End. Claim cards retain semantic keyboard activation, nested controls do
not also select the card, and status meaning never depends on color alone.

The accepted architecture and full acceptance contract live at
`.data/designs/co-work/truth-surface/README.md`.
