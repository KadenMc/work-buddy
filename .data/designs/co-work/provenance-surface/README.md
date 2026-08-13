# Co-work Provenance surface

**Status:** Accepted design and implementation contract for the first
end-to-end Provenance lens. The as-built boundary is recorded in
[implementation-plan.md](implementation-plan.md); anything listed under
**Deferred** is not part of this delivery.

## Decision

Add **Provenance** as a first-class Co-work rail beside **Review**, **Truth**,
and **Chat**. Selecting it activates a view-only provenance lens over the open
document. The lens answers a different question from the neighboring surfaces:

| Surface | Primary question | Persistent editor lens |
|---|---|---|
| **Review** | What proposed document or evaluation change needs my decision? | `review` |
| **Provenance** | Where did these words come from, who is said to have written them, and what human review is recorded? | `provenance` |
| **Truth** | What claims does this work rest on, and what evidence and lifecycle do they have? | `truth` |
| **Chat** | What do I want to discuss or ask the agent to do? | `neutral` |

The four lens values are mutually exclusive. Temporary passage emphasis used
by Chat, **Working on**, or an explicit navigation command remains independent.
A rail change only replaces view decorations and compatible focus; it never
changes the document, Y.Doc, Markdown, selection, scroll position, or undo
history.

This is a panel, not only a toolbar toggle, because provenance has durable
state and legitimate actions. The overlay makes the document scannable; hover
explains one passage; the stable panel owns filters, details, history, and
human decisions. That separation keeps a transient hover card from becoming a
fragile mutation surface.

## User jobs

The surface supports six practical jobs:

1. **Scan** the document for AI, human, mixed, unknown, unrecorded, or stale
   provenance.
2. **Understand** the source, authorship assertion, review state, attester, and
   basis for one passage without leaving the document.
3. **Record review** of an exactly targeted AI- or mixed-authored document or
   passage as the currently enrolled local user without erasing its authorship
   or source.
4. **Repair awareness** by identifying unrecorded, unresolved, ambiguous, or
   conflicting coverage rather than silently presenting it as human-authored.
5. **Capture new writing** by recording a local typing burst as an exact
   `direct_entry` span attributed to the enrolled inputter once the edit and
   its frozen structured head are durable.
6. **Repair older uncovered text** through an explicit selection-based
   determination whose source stays honestly labeled **Untracked / legacy**.

Provenance does not answer whether the prose is correct, whether a claim is a
fact, whether an edit should be accepted, or whether a named person really
wrote the words. Those questions remain with Truth, Review, or a future
identity-verification boundary.

## Four independent recorded axes

One provenance attestation keeps these facts separate:

- **Source** describes how the content entered the system and any retained
  occurrence or format facts. A paste, file import, direct entry, and accepted
  proposal are different sources.
- **Authorship** is `human`, `ai`, `mixed`, or `unknown`, with contributors when
  recorded.
- **Human review** is `reviewed`, `not_reviewed`, `not_applicable`, or
  `unknown`, with reviewers when recorded.
- **Attester and basis** identify who made the assertion and why it was
  recorded, for example user attestation, automatic short-text attribution,
  automatic direct-entry attribution, proposal acceptance, migration, or
  legacy data.

These dimensions must not be collapsed into a single trust badge. In
particular:

- AI-authored and human-reviewed remains AI-authored;
- human-authored does not automatically mean reviewed;
- a local paste gesture does not prove the clipboard text was composed by the
  person who pasted it;
- an attestation is a report, not authorship verification; and
- reviewed does not mean approved, accepted, fact-checked, correct, or safe.

The enrolled local dashboard principal and one-time, action-bound gesture make
the **review action** attributable to a durable local actor. This remains a
local authority, not a verified remote identity or authenticated multi-user
account. A typed name for someone else remains a claimed name.

## Effective state and immutable history

Attestations are append-only. A correction or review transition appends a new
record whose `supersedes_id` names the prior record; history is never updated
in place or deleted.

For a single lineage, the effective attestation is the unsuperseded leaf. A
review action is deliberately constrained:

1. the server verifies that the selected record belongs to the open document
   and is still an effective leaf for the same frozen target;
2. it derives a replacement from that record rather than accepting a
   client-authored rewrite;
3. it preserves source, authorship, contributors, and the target;
4. it changes human review to `reviewed` and records the enrolled acting user
   as reviewer and attester; its own basis is `user_attestation` and references
   the predecessor, while the predecessor retains its original automatic,
   proposal, migration, or legacy basis; and
5. it appends the replacement with a new idempotency key and a one-time gesture
   bound to the exact request.

Thus the visible transition is:

> AI-authored, not reviewed → AI-authored, reviewed by the enrolled local user

It is not AI-authored → human-authored. A stale leaf, already-superseded record,
different target, actor change, head conflict, or idempotency mismatch fails
closed and requires a refresh or a fresh user decision. Independently effective
overlapping attestations that disagree are a **conflict**, not an invitation to
use last-write-wins.

## Exact coverage semantics

The overlay is a projection over the current editor, not a mutation of its
content. It derives current coverage conservatively:

- A document-version attestation covers the whole current document only while
  its frozen structured-head digest equals the current structured head. Once
  the document changes, the old version remains inspectable history but cannot
  attribute newly edited text.
- A span attestation carries the complete exact quote selector: `exact`,
  `prefix`, and `suffix`. At its frozen head it is current. After unrelated
  document change it is **reanchored** for display only when that selector
  resolves to exactly one passage in the hydrated editor.
- A selector with no match is **unresolved**. More than one match is
  **ambiguous**. Both are stale-target states and receive no guessed range.
- Text not covered by any safely projected current or uniquely reanchored
  attestation is **unrecorded**. It must not inherit a human label merely
  because it has no decoration.
- A current document-level attestation is fallback coverage. Any current
  explicit span overrides that fallback for its exact range.
- Compatible peer explicit spans may be shown as combined coverage.
  Incompatible independently effective peer spans produce an explicit
  conflict state and expose every contributing attestation in the panel; time
  and DOM order never choose a winner.

An unsynchronized local-human edit invalidates the pulled provenance head
immediately. Until persistence settles and a matching authoritative projection
arrives, the lens withholds document-wide fallback and treats exact spans as
requiring re-anchor. A span that still resolves uniquely may remain painted for
inspection; text outside uniquely resolved recorded spans is unrecorded. This
prevents newly typed text from inheriting an older document-wide attribution.

For ordinary local typing, that invalidation is followed by a durable
direct-entry capture. The editor synchronously stages each evolving burst before
the transaction can be lost, coalesces only one contiguous same-block burst,
and updates the staged exact selector for backspace and correction. It closes
the burst at a quiescent persistence boundary, cursor or block discontinuity,
paste/drop, history action, blur, lens change, save, or teardown. After Yjs
persistence settles, the browser freezes one exact current-head request and the
server appends a human-authored, review-not-applicable span with
`basis=automatic_direct_entry_attribution`. Remote/applied Yjs changes,
format-only changes, seed operations, and arbitrary disjoint transactions do
not inherit that assertion.

Document-version and exact-span targets are different scopes. A current exact
span overrides a current document-wide determination in its range; this
specific-over-fallback precedence is not a conflict. A stale document-level
record is not a fallback for a changed current head. Two overlapping records
are compatible when their source, authorship, and human-review assertions
agree; different attesters or bases describe independent attestations and do
not by themselves create an assertion conflict. Conflicted coverage remains
recorded-but-disputed rather than being counted as unrecorded.

## Visual language

The lens uses separate channels and always repeats the meaning in text in the
hover card and panel:

| Meaning | Document treatment | Text label |
|---|---|---|
| Human authorship | subtle cool-neutral wash | Human-authored |
| AI authorship | subtle violet wash | AI-authored |
| Mixed authorship | split or patterned wash | Mixed authorship |
| Unknown authorship | neutral dotted/patterned treatment | Authorship unknown |
| Unrecorded coverage | neutral hatch/dotted treatment | No provenance recorded |
| Human reviewed | solid review underline or review mark | Human reviewed |
| Explicitly not reviewed | amber dashed underline | Not human-reviewed |
| Review unknown | gray dotted underline | Review unknown |
| Not applicable | no review warning; explicit label in details | Review not applicable |
| Unresolved, ambiguous, or conflicting | red wavy underline plus warning icon/label | Stale or conflicting provenance |

Red wavy underlining is reserved for a broken or conflicting target. It must
not mean merely “AI-authored” or “not reviewed,” because those are ordinary
recorded states rather than errors. No state may be communicated by color
alone. Patterns, underline styles, icons, and plain-language labels remain
distinguishable in forced-colors mode and at browser zoom.

Source and attester are intentionally not assigned additional simultaneous
per-character colors. There are too many source kinds, and another color
channel would make the page unreadable. They are first-class data in the hover
and stable panel and may be filtered there.

## Hover explanation

Pointer hover, or moving the keyboard caret/selection through a decorated
passage while the editor is focused, exposes a compact non-modal card
containing:

- authorship and named contributors when present;
- human-review status and reviewers;
- source kind and retained source reference when permitted;
- attester and basis;
- exact/current, unrecorded, stale, ambiguous, or conflict state; and
- a plain-language direction to open Provenance for stable details and actions.

The hover card is explanatory. It does not contain **Mark reviewed**, correction,
delete, or any other mutation. It does not steal editor selection, move either
pane, or persist its open state. It closes on Escape, pointer departure, focus
departure, lens change, or document change. Keyboard users can reach the same
information through the document-order panel list even when editor decoration
spans themselves are not convenient tab stops.

While Provenance owns the editor lens, the generic **Give feedback** affordance
is absent. A deliberate text selection instead gets one provenance-specific
entry point: **Record provenance** for uncovered text, **Review provenance** for one
exact eligible AI/mixed span, **View provenance** for one healthy recorded
target, or **Inspect provenance** for multiple, stale, ambiguous, or conflicting
coverage. The floating action never performs a review mutation: it opens the
stable panel, where the target, consequences, and guarded action remain visible.
Recording uncovered text opens the shared determination form and stores the
source as untracked legacy content rather than claiming it was just typed.

## Stable panel

The Provenance panel has three progressively disclosed levels:

1. **Summary** — needs-review and reviewed counts, issue count, and whether any
   current text is unrecorded.
2. **Document-order list** — one row per projected target or issue, with a text
   excerpt, authorship label, review label, and target-health label.
3. **Detail** — all four recorded axes, frozen target, effective state,
   target-lineage history, and currently valid actions. A separate collapsed
   **Complete provenance history** section keeps every append-only record for
   the document inspectable, including old or malformed targets which cannot
   safely project into the current text.

Initial filters are conservative and additive: **All**, **Needs review**,
**AI-authored**, and **Issues**. A filter changes the list, never the document
content or what the overlay truthfully represents. If a later implementation
adds overlay-dimension toggles, hiding a treatment must remain visibly
disclosed; this first slice does not need them.

Selecting a list row opens or closes its detail and may apply compatible focus;
it does not scroll the editor. A uniquely resolved span exposes the explicit
**Show in document** action, which issues one present-user reveal command and
must not replay after refresh. An unresolved or ambiguous row opens details
without scrolling to a guessed location. Expanding or closing inline detail
does not replace the independently persisted list position.

For an AI- or mixed-authored target, stable detail always shows the **Mark
reviewed** action. It is enabled only when all of these are true, and otherwise
stays visible with the exact reason it is unavailable:

- the record is effective and targets either the current whole-document
  version or a uniquely resolved span in the current document;
- its frozen structured-head digest still equals the current document head;
- authorship is AI or mixed;
- review is not already `reviewed`;
- the document and provenance surfaces are writable; and
- the current actor and exact target can receive a fresh bound gesture.

The action and nearby explanatory copy say what it does and does not assert.
While a request is pending it is disabled and duplicate activation is
idempotent. A successful append refreshes the authoritative read model; a
failure keeps the previous data visible and explains whether refresh, retry, or
a new decision is required.

The editor owns a persistence barrier around this mutation. It disables
editing, retries pending persistence when needed, flushes the Yjs outbox,
asserts canonical editor state, compacts to a durable structured head, and
keeps the editor locked while the Provenance provider performs a forced fresh
document pull. The panel then re-finds the same effective leaf, rechecks the
fresh head, eligibility, unique exact-span resolution, and incompatible peer
overlaps before posting. The lock remains in place through the append and its
authoritative repull; editing is restored only if the mounted document is still
writable. Any drift fails closed and asks for a fresh inspection and gesture.

## Loading, empty, error, and read-only states

- First load shows a bounded loading state, not “No provenance.”
- Background refresh retains prior data and geometry until an authoritative
  replacement arrives.
- A failed load offers **Retry** and is never rendered as an empty document.
- A document with text and no current coverage says **No provenance has been
  recorded for this document. New typing is recorded automatically; select
  existing text to record it.** The lens shows unrecorded coverage and makes
  clear that the earlier source remains untracked.
- A genuinely empty document says **This document has no text to map yet**.
- Read-only mode preserves scanning, hover, detail, and history while mutation
  controls explain why they are unavailable.
- A retired or unavailable document retains historical inspection only when
  the surrounding Co-work lifecycle permits it.

## Responsive, focus, and scroll behavior

The rail tablist uses roving keyboard focus with Left/Right arrows and Home/End
across all four tabs. The selected tab and the active lens remain synchronized.
Adding Provenance must not make **Chat** unreachable at narrow widths; the
tablist may scroll horizontally with a visible focus target rather than clip or
shrink labels into ambiguity.

The panel owns one scroll position per full Folder and document identity,
separate from Review, Truth, and Chat. Summary controls remain compact; the
document-order list receives the remaining height. Expanding or closing an
inline detail preserves that list position. Switching tabs or refreshing data
does not scroll the editor or rail. On narrow layouts, existing panel stacking
or drawer behavior is preserved; the hover card is clamped to the viewport and
must not cover the focused passage when a usable alternate placement exists.

All controls have visible focus. Status icons are decorative only when their
adjacent text already names the state. Touch has a stable panel path and never
depends on hover. Reduced-motion mode avoids animated scanning or underline
effects.

## Architecture

The implementation has four boundaries:

1. **Truth provenance authority** retains append-only attestations and
   supersession history.
2. **Co-work read/action API** returns an authoritative provenance projection
   alongside the open document and exposes the constrained review transition
   behind local identity, CSRF, exact context, and one-time gesture checks.
3. **Dedicated Provenance provider** parses typed provenance records, keeps
   stale/error state explicit, and emits one view projection plus forced
   refresh after action. It may share the open-document snapshot source with
   neighboring surfaces, but it does not enlarge `ReviewRailData` into a
   general provenance transport.
4. **Durable browser input provenance outbox** stages local typing and paste
   captures before asynchronous IndexedDB work, freezes each request only after
   Yjs persistence, and reconciles crash/reload delivery without changing the
   capture-time actor.
5. **ProseMirror decoration plugin and Provenance rail** render the view-only
   lens, re-anchor exact spans, compute uncovered text, explain hover, and own
   stable details/actions.

SSE may nudge invalidation, but the client always repulls authoritative data.
The overlay never writes a ProseMirror transaction that changes document state.
Raw database IDs and hashes stay out of routine copy, though advanced history
may expose shortened identifiers where useful for diagnosis.

## Acceptance contract

The slice is complete only when all of these are demonstrable:

1. **Review | Provenance | Truth | Chat** is keyboard-operable, and each tab
   activates exactly its intended lens without moving or mutating the document.
2. Current AI-authored, not-reviewed text is distinguishable from AI-authored,
   reviewed text by more than color.
3. Human, mixed, unknown, and unrecorded text cannot be mistaken for one
   another in the panel, and missing data never defaults to human.
4. Hover/focus details name all available axes and target health; mutations are
   absent from hover.
5. **Mark reviewed** appends a superseding record, retains AI/mixed authorship
   and source, records the enrolled reviewer/attester under a new
   user-attestation basis, preserves the predecessor's original basis in
   history, and refreshes the lens.
6. A stale, ambiguous, conflicting, already-superseded, wrong-document, or
   actor-changed action fails closed without changing history.
7. A changed document-version attestation does not paint newly edited text;
   unique exact spans re-anchor for inspection and missing/ambiguous selectors
   become issues. A reanchored changed-head span is not review-actionable in
   this slice.
8. Loading and failure are not rendered as empty; read-only mode keeps
   inspection useful.
9. Existing Review, Truth, Chat, temporary-highlight, persistence, selection,
   scroll, and undo behavior remains intact.
10. Backend, projection, decoration, interaction, accessibility, narrow-layout,
    and non-regression tests cover the matrix in the implementation plan.
11. A local edit immediately removes stale document-wide fallback; review
    stays unavailable until the editor persistence barrier and a forced fresh
    preflight agree on one exact current head and effective leaf.
12. Typing `Test` as one ordinary local burst creates one current exact
    `direct_entry` span for `Test` after persistence, including through ordinary
    backspace/correction and crash recovery; it never attributes intervening,
    remote, pasted, or pre-existing text.
13. Provenance selections never show **Give feedback**. They offer Record,
    Review, View, or Inspect according to exact current coverage, with
    durable mutation confined to the stable panel or determination form.

## Deferred

The following are explicitly outside this delivery:

- verified remote or multi-user identity and `account_ref` enrollment;
- character-level collaborative authorship inferred from remote, applied,
  programmatic, or otherwise arbitrary Yjs updates (local editor direct-entry
  transactions are captured explicitly at ingress);
- automatic propagation of version-wide attribution through later edits
  without a proven changeset map;
- durable review of a changed-head client-reanchored span; it requires a
  trusted server/editor text-geometry proof rather than trusting browser
  placement for an append-only assertion;
- a general provenance correction composer or arbitrary source/authorship
  rewriting;
- bulk **Mark reviewed**, conversational review assent, or agent-authored
  review decisions;
- automatic authorship detection, plagiarism detection, factual verification,
  or “AI detector” scoring;
- source-content browsing that bypasses Sources access/redaction policy;
- provenance overlays across multiple documents at once; and
- user-configurable color themes or per-axis overlay toggles beyond the
  accessible built-in treatment.
