# Provenance surface implementation plan

**Parent contract:** [README.md](README.md)

**Delivery status:** implemented as one end-to-end vertical slice, with backend
and frontend checkpoints that remain independently testable. This plan records
the as-built boundary; the follow-ons under **Deferred** have not shipped.

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
- [x] Record accessibility, narrow-layout, focus, scroll, loading, error, and
  read-only behavior.

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
- Apply explicit spans over current document-level fallback; combine compatible
  peer-span overlap and project incompatible peer-span overlap as conflict.
- Treat an unsynchronized local-human edit as immediate head invalidation:
  withhold the older document-level fallback, downgrade exact spans to
  re-anchor-for-inspection, and restore current coverage only after a matching
  fresh authoritative projection arrives.
- Keep prior data during background refresh and distinguish first load, empty,
  failure, and read-only state.

### 4. Editor lens and hover

- Extend `CoworkEditorLens` to `neutral | review | provenance | truth`.
- Render provenance decorations only for the provenance lens. Remove
  provenance treatment from the Truth lens so the two surfaces have clean
  ownership.
- Apply independent authorship and review classes/data attributes plus issue
  treatment. Decorations are view-only and excluded from serialized content.
- Expose compact hover/focus metadata through a clamped, dismissible card.
- Make hover passive and action-free. It points people to Provenance for stable
  details; it never performs navigation or mutation itself.
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
| Provenance model | effective leaf; linear supersession; existing fork surfaced; source/authorship preserved; reviewer/attester independent |
| Review route | success; idempotent replay; idempotency mismatch; wrong document/target; already superseded; stale head; retired/read-only; missing CSRF/session/gesture; actor change |
| Read projection | document target at matching head; document target after head drift; span selector payload; append history; malformed/conflicting records preserved as issue |
| Span resolution | unique exact/prefix/suffix; missing; ambiguous repeated quote; compatible overlap; incompatible overlap; Unicode and block boundaries |
| Coverage | all text covered; partially covered complement; entirely unrecorded; empty document; version baseline plus span refinement; stale version never covers new text; local edit immediately withholds old fallback |
| Decoration | lens exclusivity; authorship classes; independent review classes; issue override; non-color attributes; no serialization/undo/selection/scroll mutation |
| Hover | pointer and editor caret/focus open; Escape/departure/lens/doc change close; viewport clamp; complete labels; no actions |
| Panel | summary/filter/list/inline detail/target history/complete document history; passive row focus; explicit one-shot Show in document; unresolved no-scroll; expanding detail preserves list scroll; background refresh geometry; loading/error/empty/read-only |
| Rail | four-tab roving focus; Home/End; persisted pane; independent pane scroll; narrow-width reachability; Chat remains neutral |
| Action UI | editor persistence lock; forced fresh preflight; fresh-head/leaf/anchor/peer-conflict recheck; eligibility; pending dedupe; stable idempotency key across ambiguous refresh; successful authoritative repull; typed stale/actor/idempotency recovery; AI remains AI after review |
| Regression | Review/Truth/Chat lenses; temporary highlights; proposal and claim navigation; paste provenance outbox; Yjs persistence; production build and type-check |
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
