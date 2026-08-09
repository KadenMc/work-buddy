# Co-work Truth surface

**Status:** Accepted design and implementation contract.

## Decision

Co-work has three first-class rail surfaces:

1. **Review** is the modification inbox for proposals, flags, evaluation results,
   and claims that require a decision.
2. **Truth** is the place to observe and manage the claim ledger beneath the
   work.
3. **Chat** is the conversational interaction surface.

Truth is intentionally both an observability surface and a modification
surface. It is not a renamed Review queue. A person can inspect what the
document rests on without first having a pending task, then begin an appropriate
claim or connection change from the same context.

This decision narrows the earlier naming rule that treated truth as entirely
internal. **Truth** is now deliberate product language for this ledger-facing
tab. Low-level terms such as `TruthStore`, store IDs, canonical hashes, gestures,
and database rows remain implementation language rather than routine UI copy.

## Terminology

| Term | Meaning in Co-work |
|---|---|
| **Truth** | The first-class rail surface for observing and managing claims, their document expressions, provenance, and lifecycle. |
| **Claim** | A proposition recorded in the append-only Truth ledger. A claim is not automatically a fact. |
| **Fact** | A filter result, not a claim kind: a claim currently held as authoritative. It has confirmed base status, is current-valid, is not redacted or voided, and has no active needs-review overlay. |
| **Expression** | A document passage where a claim is said, with a role such as quote, paraphrase, summary, or instantiation. |
| **Evidence / receipt** | What supports the claim or what the person was shown when making a decision. An expression is not evidence merely because it contains similar words. |
| **Needs review** | An active attention overlay on a claim. It remains separate from the claim's base lifecycle status. |
| **This document** | Claims connected by expressions to the open document. |
| **Folder** | Claims in the open Co-work folder's one ledger, including claims expressed elsewhere or not connected to a document. |

Use **Truth** for the tab and **Facts** for the authoritative-current filter.
Do not introduce a separate “Facts” subsystem or use `claim_kind == fact` as the
filter definition.

## Information architecture

The rail tabs are **Review | Truth | Chat** on desktop and narrow layouts.
Truth has two independently meaningful dimensions:

- Location: **This document** by default, or **Folder**.
- State filter: **All claims**, **Facts**, **Proposed**, **Needs review**,
  **Challenged**, or **Unconnected**.

Document mode contains claims with at least one expression in the open
document. Folder mode reads the complete ledger boundary and is the only mode
that can reveal claims which are connected elsewhere or unconnected. Filters do
not silently change that boundary.

The list is scan-oriented. Selecting a claim progressively discloses:

- its exact proposition;
- base status and the separate needs-review state;
- claim kind and validity interval;
- creator and relevant human decisions;
- all connected passages and each expression role;
- supporting evidence and review receipts; and
- append-only lifecycle history.

When one claim has several expressions, each expression has its own passage
navigation action. A claim-level focus may highlight every expression, but
navigation must never collapse them into an unexplained jump to the first one.
An expression in another document opens that document by its full identity and
carries its exact quote selector only through that switch. Once the destination
editor is ready, the passage is revealed once and the transient request is
discarded; a failed open also discards it so a later visit cannot replay it.

Primary modification entry points are **Propose from selection** and **Connect selection**.
Actions on an existing claim are contextual and state-valid. Truth must not
present an always-visible generic row of lifecycle verbs.

## Relationship to Review

Review is optimized for “What needs my decision?” Truth is optimized for “What
does this work currently rest on, and how did it get that status?”

Only actionable claim states are cross-listed into Review:

- proposed claims;
- challenged claims; and
- claims with an active needs-review overlay.

Routine confirmed facts and terminal history do not clutter Review. Cross-listed
cards and Truth records share the same permanent claim identity, so a decision
made from either legitimate surface refreshes both projections. Truth does not
duplicate edit proposals, Verify setup, or Co-think controls.

The cross-list lives inside Review's existing scroll body. It must not become an
unbounded fixed block above that body that compresses the proposal workspace.

## Editor lens semantics

The editor has an explicit view-only lens state:

- `review`: tracked proposals, flags, and evaluation-result anchors;
- `truth`: expression anchors and provenance treatments; or
- `neutral`: neither persistent ledger overlay.

The active rail surface controls the default lens: Review selects `review`,
Truth selects `truth`, and Chat selects `neutral`. Temporary passage highlights
from Chat, Working on, or an explicit navigation command are independent of the
lens.

Changing lens is a display operation only. It must not:

- change the ProseMirror document or Y.Doc;
- alter Markdown or create undo history;
- rewrite the editor selection;
- scroll either pane; or
- replay a previously completed navigation command after refresh.

Lens changes clear an incompatible persistent focus. A present-user activation
of a claim or expression is a one-shot navigation command that reveals the
exact passage. Passive data refresh only restores compatible visual emphasis.

## Architecture and authority

Truth uses its own read model/provider rather than enlarging Review's data
contract. The projection preserves full claim lifecycle state, including
terminal states, and carries `base_status` separately from `needs_review`.
Document mode joins that projection to expressions for the current document.
Folder mode reads the whole selected ledger. SSE remains an invalidation signal;
the client repulls authoritative state.

All modifications use canonical Truth and Co-work operations. The UI does not
write status rows, expressions, or receipts directly. Human decisions remain
bound to the exact claim and displayed context. For Co-work, that hash-bound
context includes lifecycle and review state, support and premise assessment,
conflicts and derivations, source-integrity state, and prose connections as
well as the canonical claim and receipts. Any change requires a fresh review.
Agents may propose, but cannot approve their own claims or edits.
Confirm and Reaffirm must also match the claim's exact lifecycle state inside
that write transaction. Quarantined-only support remains fully observable, but
confirmation is unavailable until a dedicated quarantine-override ceremony is
designed.

Connecting a passage records where a claim is expressed. It does not edit the
prose and does not manufacture evidence. Creating a claim from selected prose
may create a proposed claim and an expression, but the selection is still not
proof of the proposition.

Read-only or temporarily unavailable mutation authority does not disable
observability. People can still browse claims, inspect history and provenance,
and navigate to passages. A disabled action states the actual reason.

## Deliberate limitations

Expressions are append-only and there is no safe expression unlink or
retraction lifecycle yet. The surface therefore does not offer **Remove
connection**, delete an expression row, or disguise deletion as an edit. An
append-only correction model must be designed before unlinking becomes a user
action.

Challenge and supersede are deeper workflows, not one-click statuses:

- challenge requires a supported competing or conflicting claim and its basis;
- supersede requires a successor claim, relationship, reason, and appropriate
  human authority; and
- redaction is destructive content removal and therefore exists only as a
  dedicated warning, reason, and exact hash-bound confirmation flow.

The surface does not expose placeholder challenge or supersession operations.
Their focused flows can extend the detail view without changing the Truth
tab's list, identity, or lens architecture. Redaction remains deliberately
separate from a generic lifecycle button.

## Persistence, states, and accessibility

Persist the selected rail tab, Truth location, active filter, and an independent
Truth scroll position per full folder and document identity. Restoration is
interface continuity, not document state. It must never replay a passage jump.
List scroll is stored separately from transient details and selection forms:
drill-ins begin at the top, returning restores the list, and changing location
or filter resets the new list to the top. The client consumes every backend
page so Folder browsing, connection choices, and Review cross-listing never
silently stop at an arbitrary first-page limit.

Initial loading keeps the rail geometry stable. A background refresh retains
the prior list, selection, filters, and scroll position. First-load failure
shows a retry action; refresh failure shows stale data with a non-destructive
warning. Empty states are distinct:

- no document connections: **No claims are connected to this document** with
  **Connect selection**;
- no claims in the folder: **No claims in this folder** with **Propose from selection**; and
- no filter matches: a filter-specific empty message with **Clear filters**.

Desktop and narrow tab lists use the same keyboard model: one tab stop, Arrow
keys, Home/End, and labelled tab panels. Claim cards are efficient whole-card
pointer targets with a semantic keyboard control. Nested actions do not also
select the card. Status and connection meaning never rely on color alone.
Forms manage and restore focus, filter counts are announced, mutation success
uses a polite announcement, and blocking failures use an alert.

## Acceptance contract

The slice is acceptable only when all of the following hold:

1. **Truth** appears as a peer of Review and Chat in desktop and narrow layouts.
2. **Facts** returns only current authoritative claims under the definition
   above, regardless of claim kind.
3. This document and Folder boundaries produce different, truthful results,
   including an unconnected Folder claim.
4. Proposed, challenged, and needs-review claims cross-list into Review by the
   same claim ID; routine confirmed and terminal claims do not.
5. A claim with multiple expressions lists and navigates to each exact passage.
6. Switching Review, Truth, and Chat changes only editor decorations. Serialized
   ProseMirror/Yjs content, selection, and scroll position remain unchanged.
7. New claim and Connect selection preserve human/agent authority and never
   turn selected prose into fabricated evidence.
8. Read-only mode retains complete observation and navigation while explaining
   unavailable writes.
9. Reload restores the tab, location, filter, and independent Truth scroll;
   refresh and SSE do not snap either pane or replay navigation.
10. Loading, first-load error, refresh error, the three empty states, forced
    colors, keyboard navigation, and focus return are covered by automated or
    live acceptance tests.

## Extension points

The architecture admits focused challenge, supersede, expression correction,
richer receipt inspection, claim comparison, multi-user actor selection, and
broader redaction subjects. Those workflows extend the detail surface and canonical operations.
They do not turn Truth into a second Review queue, overload Chat, or change the
claim/expression/evidence distinction.
