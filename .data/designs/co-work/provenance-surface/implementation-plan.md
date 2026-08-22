# Provenance surface implementation plan

**Parent contract:** [README.md](README.md)

**Delivery status:** implemented as an end-to-end provenance lens plus
direct-entry capture, pending-delivery presentation, selection repair, and
accepted-agent-proposal attribution. Backend, durable input delivery, sitting
integration, and frontend checkpoints remain independently testable. This plan
records the as-built boundary; the follow-ons under **Deferred** have not
shipped.

## Workstreams and sequence

### 0. Freeze the contract

- [x] Give Provenance a first-class rail boundary and a mutually exclusive
  `provenance` editor lens.
- [x] Preserve source, authorship, review, and attester/basis as independent
  axes.
- [x] Define current, unrecorded, unresolved, ambiguous, and conflicting
  coverage without last-write-wins inference.
- [x] Define **Mark reviewed** as an append-only constrained supersession.
- [x] Reserve hover for explanation and stable detail for action.
- [x] Give ordinary local typing a durable exact-span provenance path and give
  uncovered historical text an honest explicit repair path.
- [x] Replace generic feedback affordances in the Provenance lens with one
  coverage-aware provenance selection action.
- [x] Record accessibility, narrow-layout, focus, scroll, loading, error, and
  read-only behavior.
- [x] Present an exact local direct-entry capture as **Recording provenance…**
  without inventing authorship or review while the ledger receipt is pending.
- [x] Integrate accepted agent proposals into the detailed provenance ledger
  transactionally, without treating acceptance as human review or relabeling
  preserved original text.

### 1. Authoritative backend projection

Produce a typed, additive provenance projection for the open document. The
server projection should carry enough immutable data for the client to resolve
exact spans and explain history without reconstructing database semantics.

Required fields per attestation or effective item:

- permanent attestation ID and `supersedes_id`;
- target kind, document/version/span identity, complete quote selector for
  spans, and frozen structured-head digest;
- source object;
- authorship kind and contributors;
- human-review state and reviewers;
- attester, basis, creation time, and canonical digest;
- effective/superseded state and target-head relation; and
- history or a stable way to join it.

Projection rules:

- identify effective leaves without deleting history;
- reject malformed/forked supersession from mutation paths and surface any
  pre-existing inconsistency as an issue;
- expose document-version coverage as current only at the same structured head;
- leave changed-head exact span re-anchoring to the hydrated editor, which owns
  current text geometry, and mark successful client reanchors inspectable but
  not review-actionable;
- use a current document-level record as fallback coverage, let explicit spans
  override it, and preserve incompatible peer-span conflicts rather than
  selecting by time; and
- remain additive to existing document reads so older clients can ignore it.

As built, `GET /api/truth/doc/<document_id>?store_id=...` retains the legacy
`authorship_attestations` field and adds `provenance` with schema
`cowork-provenance-view/v1`. That view carries the current structured head, an
optional document fallback, explicit span targets, complete document history,
and diagnostic summary counts. Each target carries currentness, resolution,
review eligibility, an optional issue, effective leaves, and lineage history.
Missing or malformed target rows remain visible as issues and in complete
history instead of breaking or disappearing from the read.

### 2. Constrained review action

Add or extend one document-scoped route for **Mark reviewed**. The command
contains the selected effective attestation identity, current target/head
precondition, and idempotency key. The server:

1. authenticates the enrolled local session and CSRF boundary;
2. consumes a one-time gesture bound to operation, Folder, document, actor,
   target, expected head, prior attestation, and exact body;
3. verifies the prior record is an effective leaf on the same document and
   target;
4. rechecks current target validity under the document/Yjs lifecycle locks;
5. derives the new record by preserving source, authorship, contributors, and
   frozen target;
6. sets review to `reviewed`, reviewer to the acting enrolled principal, basis
   to `user_attestation` with a reference to the predecessor, and
   `supersedes_id` to the prior record; the predecessor retains its original
   automatic, proposal, migration, or legacy basis in history;
7. appends idempotently and emits the normal provenance invalidation event; and
8. returns a compact receipt, after which the client repulls the authoritative
   document projection.

Before that request, the editor-owned mutation barrier disables editing,
retries and flushes pending persistence, verifies canonical state, and compacts
the current Y.Doc to one durable structured head. While the editor remains
locked, the dedicated Provenance provider forces a fresh document pull. The
panel re-finds the same effective leaf and rechecks the returned head,
eligibility, unique span resolution, and incompatible peer overlap. The lock
stays held through the append and the authoritative post-action repull. Any
drift fails closed before a review attestation can be appended.

No route in this slice accepts “make this human-authored” as a side effect of
review. An actor change, stale head, stale selector, already-superseded record,
fork attempt, wrong document, retired document, or idempotency collision gets a
typed failure.

The as-built route is
`POST /api/truth/doc/<document_id>/authorship-attestations/<attestation_id>/human-review?store_id=...`.
Its exact gesture-bound body contains `attestation_id`,
`expected_structured_head_sha256`, and `idempotency_key`. A successful fresh
append or an idempotent replay returns the portable superseding attestation;
the client does not clear the retry key until a forced authoritative refresh
actually contains the reviewed successor.

### 2a. Accepted agent-proposal attribution

Integrate accepted edit proposals into the same detailed span ledger rather
than relying on the proposal row or an expression span as an implicit
authorship claim. For each applied `confirm` decision on an agent-run proposal,
the sitting commit callback:

1. verifies the consumed human gesture matches the immutable proposal and its
   applied status;
2. validates the proposal's producing agent-run reference and producer
   metadata;
3. derives only text introduced by the accepted replacement: the whole
   replacement when it does not preserve the original quote, or the
   non-whitespace segments surrounding one uniquely preserved original quote;
4. proves each derived exact selector resolves uniquely in the committed
   rendered projection;
5. appends one exact-span attestation per attributable segment with
   `authorship=ai`, `human_review=not_reviewed`, and
   `source`/`basis=proposal_acceptance`;
6. binds source detail to the proposal and replacement digests, consumed
   acceptance gesture, producing run, and segment index/count; and
7. returns the attestation IDs in the sitting receipt and emits normal
   provenance invalidation events.

The accepting human is the attester but is not recorded as a reviewer merely
for accepting the edit. Deletions add no text span. A repeated preserved quote,
invalid anchor or producer metadata, mismatched gesture/status, or ambiguous
committed selector raises `proposal_provenance_unsafe`. Because this work runs
inside the materialization commit callback, that failure rolls back the
document materialization, proposal status, gesture consumption, sitting
receipt, and every provenance row together.

### 3. Frontend contracts and projection

- Extend the typed document payload to retain rich attestations/effective
  provenance rather than dropping them into the older three-state trust span.
- Use a dedicated Provenance provider and panel projection. The provider shares
  the authoritative open-document snapshot source, including sequence-guarded
  refresh publication, but does not turn Review's domain contract into a
  general ledger transport.
- Re-resolve complete quote selectors against the hydrated ProseMirror
  document and classify current-head, uniquely reanchored, absent, and
  ambiguous results. A unique reanchor is paintable/inspectable but is not
  sufficient authority for a durable review action in this slice.
- Treat current document-version coverage as a baseline only when its frozen
  head matches the current head.
- Compute uncovered text-node ranges as `unrecorded`; never infer human
  authorship from the absence of a span.
- Project uniquely resolved browser-local direct-entry captures as a distinct
  `pending` delivery state over their exact ranges. Do not attach provisional
  authorship/review semantics, and let authoritative server coverage win any
  overlap before local outbox cleanup finishes.
- Retain the exact frozen request and pending treatment until a fresh history
  entry matches the receipt's attestation ID, document-span ID, and structured
  head. Missing, stale, misbound, or failed refreshes remain retryable.
- Apply explicit spans over current document-level fallback; combine compatible
  peer-span overlap and project incompatible peer-span overlap as conflict.
- Treat an unsynchronized local-human edit as immediate head invalidation:
  withhold the older document-level fallback, downgrade exact spans to
  re-anchor-for-inspection, and restore current coverage only after a matching
  fresh authoritative projection arrives.
- Keep prior data during background refresh and distinguish first load, empty,
  failure, and read-only state.

### 3a. Durable direct-entry and manual-selection writes

- Generalize the existing paste recovery journal and IndexedDB delivery queue
  into a content-input provenance outbox with explicit `paste`, `direct_entry`,
  and `legacy` source kinds.
- Capture local inserted-text transactions synchronously before their only
  in-memory shape can be lost. Exclude paste/drop, applied remote Yjs,
  undo/redo, seed/system work, formatting-only changes, deletions outside an
  open burst, and disjoint edits which cannot be represented by one honest
  quote anchor.
- Coalesce only a contiguous same-textblock typing burst. Map its range through
  backspace and correction, cancel it when fully deleted, and close it only at
  a quiescent persistence or interaction boundary.
- Keep the capture-time determination immutable. A later identity/session must
  never take authorship for keystrokes made under a different or unavailable
  actor; actor changes require an honest fresh determination.
- Reconcile the newest synchronously staged capture over an older unfrozen
  `capturing` row after a crash, but never overwrite a ready or frozen request.
- Flush Yjs, freeze the exact head and selector, require a unique server-bound
  span, and retain the immutable request through ambiguous transport.
- Feed current `direct_entry` captures from both the durable outbox and
  mounted-page volatile recovery state into the editor's local pending
  projection; update the open burst before the edited frame paints.
- Let explicit selection repair use `source=legacy` and
  `basis=user_attestation`; never relabel pre-existing uncovered text as recent
  direct entry.
- Add Truth schema v10 support for
  `automatic_direct_entry_attribution`, retaining all v9 records, lineage,
  canonical digests, idempotency, triggers, and portable export behavior.

### 4. Editor lens and hover

- Extend `CoworkEditorLens` to `neutral | review | provenance | truth`.
- Render provenance decorations only for the provenance lens. Remove
  provenance treatment from the Truth lens so the two surfaces have clean
  ownership.
- Apply independent authorship and review classes/data attributes plus issue
  treatment. Decorations are view-only and excluded from serialized content.
- Give pending delivery its own informational treatment and
  `recordState=pending`, without applying unknown-authorship or unknown-review
  styling. Only a uniquely resolved exact local capture is paintable.
- Expose compact hover/focus metadata through a clamped, dismissible card.
- Make hover passive and action-free. It points people to Provenance for stable
  details; it never performs navigation or mutation itself.
- For pending delivery, replace the normal facts grid with **Recording
  provenance…** and a statement that authorship and review await the server
  receipt; do not expose placeholder facts as assertions.
- Suppress the generic **Give feedback** selection affordance whenever the
  Provenance lens owns the editor. Show one coverage-aware action instead:
  Record, Review provenance, View, or Inspect. Review provenance routes to stable detail
  rather than mutating from a transient bubble.
- Preserve temporary Chat/Working-on highlights and clear incompatible
  persistent focus on lens changes.
- Verify no document transaction, selection rewrite, scroll, or undo entry is
  introduced by a lens or hover change.

### 5. Rail and stable panel

- Insert **Provenance** between Review and Truth in the shared roving tablist.
- Persist the selected pane and an independent Provenance list scroll per full
  Folder/document identity.
- Implement summary counts, filters, document-order rows, target-lineage
  detail/history, complete document history, and empty/error/read-only states.
- Keep list-row activation to detail and compatible focus. Reveal a uniquely
  resolved span only from the explicit **Show in document** action and only
  once; unresolved/ambiguous items open detail without a guessed scroll.
- Place **Mark reviewed** only in stable detail and enable it only for an
  effective, writable, exact-current-head AI/mixed document-version or span
  target not already reviewed.
  A uniquely reanchored changed-head span explains why it is inspect-only.
- Keep Mark reviewed visible but disabled with a reason for AI/mixed targets
  which are not currently eligible. Give zero-record text one useful empty
  state that explains new typing is automatic and existing text can be selected
  for an explicit determination.
- While any local direct-entry capture is pending, show a polite
  **Recording provenance for recent typing…** status, report unrecorded summary
  state as **Updating…**, and suppress definitive zero/unrecorded copy for that
  pending interval.
- On success, retain geometry while repulling; on failure, retain the previous
  projection and give a typed recovery path.
- Ensure all four tabs and every action remain usable in narrow layout, at 200%
  zoom, by touch, and by keyboard.

### 6. Documentation and release evidence

- Update `cowork/content-provenance` with the read/action surface and enrolled
  local-review semantics.
- Update `cowork/truth-surface` so provenance is a peer boundary and the Truth
  lens owns expressions, not document-authorship treatment.
- Validate agent knowledge and rebuild its index.
- Run focused backend/frontend tests, TypeScript type-check, production build,
  `git diff --check`, and the relevant existing Co-work regression lanes.
- Record actual commands and outcomes in the PR handoff. Do not convert an
  unrun or timed-out check into a pass.

## Test matrix

| Layer | Required cases |
|---|---|
| Provenance model | effective leaf; linear supersession; existing fork surfaced; source/authorship preserved; reviewer/attester independent; strict source/basis pairing; schema v9→v10 lineage preservation |
| Proposal acceptance | agent-run producer validation; AI/not-reviewed semantics; acceptance distinct from review; proposal/replacement/gesture/run linkage; exact replacement; insertion before/after preserved original; whitespace trimming; deletion; repeated original; ambiguous selector; transaction rollback; idempotent segments |
| Review route | success; idempotent replay; idempotency mismatch; wrong document/target; already superseded; stale head; retired/read-only; missing CSRF/session/gesture; actor change |
| Read projection | document target at matching head; document target after head drift; span selector payload; append history; malformed/conflicting records preserved as issue |
| Span resolution | unique exact/prefix/suffix; missing; ambiguous repeated quote; compatible overlap; incompatible overlap; Unicode and block boundaries |
| Coverage | all text covered; partially covered complement; entirely unrecorded; empty document; version baseline plus span refinement; stale version never covers new text; local edit immediately withholds old fallback |
| Decoration | lens exclusivity; authorship classes; independent review classes; exact pending range; pending carries no authorship/review classes; authoritative span wins pending overlap; issue override; non-color attributes; no serialization/undo/selection/scroll mutation |
| Hover | pointer and editor caret/focus open; Escape/departure/lens/doc change close; viewport clamp; complete labels; pending-delivery copy without placeholder assertions; authoritative replacement in place; no actions |
| Panel | summary/filter/list/inline detail/target history/complete document history; pending banner and Updating summary; pending suppresses definitive unrecorded copy; passive row focus; explicit one-shot Show in document; unresolved no-scroll; expanding detail preserves list scroll; background refresh geometry; loading/error/empty/read-only |
| Rail | four-tab roving focus; Home/End; persisted pane; independent pane scroll; narrow-width reachability; Chat remains neutral |
| Action UI | editor persistence lock; forced fresh preflight; fresh-head/leaf/anchor/peer-conflict recheck; eligibility; pending dedupe; stable idempotency key across ambiguous refresh; successful authoritative repull; typed stale/actor/idempotency recovery; AI remains AI after review |
| Direct entry | one `Test` burst; idle/lens/blur close; backspace/correction; full deletion; cursor/block split; paste/drop/history/remote/format exclusion; disjoint-range rejection; capture-time actor retention; missing identity; crash after staged update; unmount/reopen; frozen request replay; delayed recorder remains pending until receipt |
| Selection UX | uncovered Record; exact eligible Review provenance routing; healthy View; stale/multiple/conflict Inspect; no Give feedback; manual legacy determination; narrow editor-pane reachability |
| Regression | Review/Truth/Chat lenses; temporary highlights; proposal and claim navigation; content-input provenance outbox; Yjs persistence; production build and type-check |
| Accessibility | state names without color; forced-colors; visible focus; keyboard-only detail/action; touch path; 200% zoom; reduced motion |

## End-to-end acceptance scenarios

### AI passage becomes reviewed

Given a uniquely resolved exact-current-head span recorded as AI-authored and
not reviewed,
when the enrolled user opens its stable Provenance detail and chooses **Mark
reviewed**, then the server appends a superseding attestation, the refreshed
overlay retains AI authorship, the review treatment changes to reviewed, and
history shows both records and the enrolled reviewer.

### Changed document-level target

Given a document-version attestation and a later document head, when Provenance
opens, then the old record remains available in history but it does not color
the changed current document as if every word retained that attribution.
Current text without another exact record is unrecorded.

### Duplicate quote

Given one changed-head span selector which now resolves to two passages, when Provenance
opens, then neither passage is guessed. The panel reports ambiguous/stale
targeting, red wavy issue treatment is used only where a safe issue anchor
exists, and **Mark reviewed** is disabled until a fresh exact target is made.

### Overlap conflict

Given two independently effective overlapping spans with incompatible source,
authorship, or review assertions, when the lens projects them, then the overlap
is a conflict and detail lists both records. Creation time does not select a
winner.

### Failure is not emptiness

Given prior provenance data and a failed refresh, when the request fails, then
the prior overlay and panel geometry remain visible with an error and Retry.
The UI never says there is no provenance merely because the server was
unavailable.

### Local edit before review

Given a pulled document-wide attribution and a later local-human edit, when the
edit has not yet settled to a matching authoritative head, then the old
document-wide record does not paint the new text and **Mark reviewed** is
unavailable. When the person invokes review, the editor is locked while
persistence settles and Provenance is forcibly refreshed; the action continues
only if the fresh head, effective leaf, unique target, and peer-conflict check
all still agree.

### Historical record without current geometry

Given an append-only record whose old or malformed target cannot project into
the current editor, when Provenance opens, then it remains inspectable under
**Complete provenance history** without receiving a guessed range or becoming
review-actionable.

### Lens non-mutation

Given an editor with selection, scroll, and undo history, when the user cycles
Review → Provenance → Truth → Chat and opens/closes hover details, then content,
Y.Doc, selection, scroll, and undo history are unchanged; only decorations and
compatible persistent focus differ.

### Newly typed text is recorded

Given a current enrolled local inputter and an editable document, when the user
types `Test` as one contiguous burst and switches to Provenance, then the edit
is first durable in Yjs, one exact `direct_entry` span is appended at the same
head with human authorship and review not applicable, the provider refreshes,
and the passage no longer appears as unrecorded. Backspace and correction update
that same burst; deleting it completely leaves no attestation.

### Pending delivery is not unrecorded

Given a delayed direct-entry recorder, when the user types a uniquely anchored
passage and opens Provenance before the server receipt, then only that exact
passage shows **Recording provenance…**, hover and panel make no provisional
authorship or review assertion, and definitive unrecorded copy is suppressed.
When the authoritative span arrives it replaces pending treatment in place,
even if local outbox cleanup is still finishing.

### Accepted agent edit records its own text

Given an agent-run proposal whose replacement inserts a generated sentence
before one preserved original quote, when the enrolled user confirms the exact
proposal and the sitting commits, then the inserted sentence receives an exact
AI-authored, not-reviewed attestation with
`source`/`basis=proposal_acceptance`, the producing run and consumed human
gesture remain linked, and the preserved quote keeps its prior provenance.
If the attributable segment cannot be derived and resolved without guessing,
the sitting and document change do not commit.

### Existing uncovered text is repaired honestly

Given text created before direct-entry capture existed, when the user selects
it in Provenance, then the transient action says **Record provenance**, the
shared determination form names the selected passage, and the resulting exact
span keeps `source=legacy` / **Untracked**. The system never infers that the
current user typed historical text.

## Delivery gates

The PR is ready only after:

- every acceptance scenario above has automated coverage or an explicitly
  recorded manual reason;
- backend and frontend focused suites pass;
- TypeScript type-check and production build pass;
- agent documentation validates and the knowledge index rebuilds;
- `git diff --check` passes;
- no `.claude` setting or generated document-kernel runtime artifact is staged
  with this feature; and
- the handoff names deferred work without implying it shipped.

## Deferred follow-ons

The deferred list in [README.md](README.md) is normative. The highest-value
follow-ons are a general append-only correction ceremony, authenticated
multi-user reviewers, proven change-map propagation, richer authorized source
inspection, and user-configurable visual channels. None should be smuggled into
the first review action or inferred from arbitrary editor changes.
