---
name: Co-work Truth surface
kind: system
description: First-class Co-work rail surface for observing and managing the claims, expressions, provenance, and lifecycle beneath a document or Folder.
summary: Truth is the AI-assisted domain workspace for discovering, grounding, deciding, and maintaining claims, expressions, evidence, provenance, and lifecycle beneath a document or Folder. Analyze passage prepares atomic claims and may perform bounded guarded web research; manual claim and connection authoring are secondary. Truth owns full domain review, Review cross-lists attention, Chat remains a peer interaction surface but does not yet start or steer Truth analysis, and Verify remains criteria evaluation.
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
- ai-assisted
- mixed-initiative
- claim-analysis
- evidence-retrieval
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

  AI analysis must stage durable typed candidates outside the append-only
  Truth ledger. A model result cannot mint a claim, expression, evidence
  receipt, or fact before the applicable human action. Validate exact target
  identity, schema, anchors, provider authorization, source policy, and output
  hashes server-side. This staging boundary is mandatory while expressions
  have no safe correction/retraction lifecycle.

  Truth owns the full candidate/evidence review. Review cross-lists durable
  claim attention and may offer a compact analysis-ready handoff. Starting,
  steering, or explaining Truth analysis through Chat is a later extension;
  when built, it cannot implicitly accept or confirm. Verify remains
  exact-target criteria evaluation. Any prose change goes to Review as an
  ordinary immutable proposal.

  Never call a document expression, citation marker, matching claim, or search
  result supporting evidence by implication. Preserve evidence candidates,
  captured receipts, source trust/integrity, and support relationship as
  separate states. Disclose actual provider, content egress, source classes,
  fallback, and cost behavior; never claim execution was local unless the
  authorization guarantees it.
---

# Co-work Truth surface

Truth is a first-class user surface within Co-work. The rail tabs are
**Review | Truth | Chat**:

- **Review** answers “What needs my decision?” and remains the inbox for edit
  proposals, flags, evaluation results, and cross-listed claim attention.
- **Truth** answers “What does this work rest on?” and owns both observation
  and the complete domain review for claim, expression, and evidence work.
- **Chat** remains the general conversational interaction surface. Starting,
  steering, clarification, and explanation for Truth-analysis runs are later
  integrations.

The product-visible **Truth** name is deliberate. Internal implementation terms
such as `TruthStore`, hashes, raw IDs, gestures, and rows remain out of routine
copy.

Truth is not primarily a manual ledger-authoring form. Its default
mixed-initiative interaction has AI prepare exact typed work for human review.
Direct claim and connection authoring remain secondary escape hatches.

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

A **candidate claim** is a proposition prepared by an analysis run and is not
yet a ledger claim or fact. An **evidence candidate** is a retrieved source
passage which has not yet been selected and captured as an immutable evidence
receipt. A support assessment does not itself confer fact status.

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

## User jobs and staged workflows

Truth serves four user jobs:

1. **Discover** the checkable claims expressed by a passage or document.
2. **Ground** each claim in exact permitted evidence, including qualifying or
   contradictory passages.
3. **Decide** which prepared claim, expression, and evidence work should enter
   or change Truth.
4. **Maintain** affected Truth after prose, evidence, or claim foundations
   change.

The first slice and primary action is **Analyze passage**. AI uses one exact
selected passage plus permitted surrounding context to select, disambiguate,
and decompose atomic candidate claims, infer claim kind and expression role,
match likely existing claims, and run bounded guarded web research when it
would materially help assess a factual claim. Each run may issue three queries,
admit five lead-only hits per query, and fetch only its own server-issued hit
IDs. The user receives prepared cards rather than a verbatim copy in a manual
form. **Add claim manually** and **Connect selection manually** stay available
under a secondary Add or overflow affordance.

The exact selected passage is capped at 32 KiB of UTF-8. Existing Truth context
is capped at 32 KiB serialized, including at most 200 claims and 200 recorded
support receipts under smaller claim and receipt sub-budgets; the complete
worker context is capped at 90,000 bytes. Normalized output is capped at 80,000
bytes, 20 claim candidates, and 10 evidence candidates per claim. The selected
account-model worker session has a provider-enforced $2.00 hard ceiling.
Guarded web search and fetch are separate provider-dependent egress with no
enforced monetary ceiling; their activity limits do not guarantee provider
cost.

Later slices widen this contract to **Analyze document**, **Find evidence**,
**Build from source**, and change-driven maintenance. **Find evidence** remains
the dedicated claim-contextual research workspace for explicit source
selection, steering, broader Folder and connected sources, and ongoing evidence
management. Analyze passage reports what its bounded run actually searched,
failed to search, or did not search from durable receipts; it does not pretend
the later workflow or an unqueried source class already ran.

## Review cross-listing

Only claims requiring attention are cross-listed into Review: proposed,
challenged, and active needs-review claims. Ordinary confirmed facts and
terminal history remain observable in Truth without filling the modification
inbox. Both projections use the same permanent claim ID, so a legitimate
decision refreshes both surfaces.

Truth owns the complete domain review for prepared claims, inferred
expressions, evidence candidates and receipts, support assessments, conflicts,
and lifecycle decisions. Review may carry one compact handoff when an analysis
is ready, but it does not duplicate that workspace. Any prose correction
prepared while grounding or maintaining a claim becomes an ordinary immutable
proposal in Review.

Cross-listed Truth attention belongs inside Review's existing scroll body. An
unbounded claim list must never sit above that body and compress the proposal
workspace.

Truth does not host edit proposals, Verify setup, Co-think controls, or a second
conversation. Starting, steering, or explaining Truth analysis through Chat is
a later extension, not part of the current Analyze-passage slice. That future
path must carry the exact target and must not let conversational text accept a
candidate or confirm a claim. Verify remains criteria checking; Truth analysis
builds and maintains the claim-expression-evidence record.

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

Truth's primary creation workflow is **Analyze passage**. It creates one
durable typed analysis run and stages prepared candidates in operational state
outside the append-only Truth ledger. Each candidate binds the exact captured
target and hashes, proposed proposition and kind, expression selector and
inferred role, possible existing-claim match, available recorded support, and
explicit ambiguity or limitations. Raw model prose is not a candidate until
the server validates schema, identity, anchors, authorization, and output
hashes.

No claim, expression, evidence receipt, or fact is created merely because AI
prepared it. Accepting a new item calls the canonical operation to create a
proposed claim and expression; accepting a match connects the passage to the
existing claim. Manual connection records where a claim is expressed without
editing prose or inventing support. The selected document passage remains an
expression, not evidence.

Mutations call canonical Truth/Co-work operations. Agents may propose and
humans retain confirmation authority over exact content and displayed context.
That decision context includes lifecycle and review state, support and premise
assessment, conflicts and derivations, source-integrity state, and prose
connections—not only the claim text and receipt rows. Any drift requires a
fresh review.

Only an exact human decision can confirm a claim. Editing a candidate changes
the payload and requires a fresh binding. Surrounding context may resolve a
reference but cannot silently widen the expression span. There is no Confirm
all; each confirmed proposition has its own exact gesture.

Provenance keeps preparation separate from addition. Candidate output is
AI-prepared and bound to its run, provider/model authorization, and hashes. A
later **Add as proposed** or connection consequence is authored by the human
who made that decision while retaining run and candidate metadata. A selected
web source keeps its `agent_run` acquisition actor and external-quarantined
origin; the person authors the support decision rather than laundering the
acquisition as human-created.

Operational runs persist only `prepared`, `launching`, `running`, `completed`,
`unavailable`, and `failed`; the UI projects the first two as **queued** and
`unavailable` as **failed**. Every run has a thirty-minute execution deadline.
An overdue active run is terminalized as `failed` with
`execution_deadline_exceeded`, including when a worker-context read discovers
the timeout. Candidates persist as `pending`, `saved`, or `dismissed`. Stale is
a decision-time validation failure, not a stored candidate status: the server
rejects the action and the person reruns analysis.

Worker submission returns only a compact receipt containing `ok`, schema
`wb.cowork.truth-analysis-submit-receipt/v1`, `analysis_run_id`, public
`status`, and `output_sha256`. Candidate content remains in the durable run
projection; the receipt does not imply a Truth-ledger consequence.

Evidence research follows permitted sources from existing claim receipts to
cited and Folder artifacts, configured connected sources, and the open web
only when authorized. Analyze passage's current web seam persists lead-only
search results, fetches only run-admitted hits, validates and pins every public
HTTP(S) destination and redirect, permits only HTTP port 80 and HTTPS port 443,
and preserves exact bounded text plus source, digest, acquisition, and
truncation metadata in a run-owned receipt. Fetch is bounded to five redirects,
twenty seconds total, ten seconds per request, and 512 KiB of identity-encoded
response. Model-facing text is capped at 64 KiB; a partial capture records
`text_truncated`, full and captured byte counts, and both full-extracted and
captured-content digests. Keep source trust/integrity separate from the assessed
relationship: supports, partially supports, contradicts, does not address, or
inconclusive. Failure to find evidence is not contradiction, and a citation
marker or search snippet is not a receipt. Only human-selected exact source
passages become Truth-ledger evidence spans and support links; external
acquisition retains actor/model, integrity, and quarantine provenance.

Analysis copy reports the actual provider, content egress, source boundary,
fallback, and cost behavior. It never says work ran locally unless the executed
authorization guarantees that fact.

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

Analysis progress, prepared candidates, and review position survive refresh,
tab changes, and sidecar restart without replaying editor navigation. A changed
document or source is revalidated at decision time; when the exact target cannot
be proven, the action fails as stale and the person reruns analysis. The server
never silently rebases. Opening Truth or typing does not start a model or web
search. Deterministic hashes, fingerprints, integrity, and
dependency state first identify affected work; semantic re-evaluation remains
an explicit bounded action.

Distinguish no document connections, an empty Folder ledger, and no filter
matches. The rail tablist uses roving keyboard focus with Arrow keys and
Home/End. Claim cards retain semantic keyboard activation, nested controls do
not also select the card, and status meaning never depends on color alone.

The accepted architecture and full acceptance contract live at
`.data/designs/co-work/truth-surface/README.md`. The staged AI-assisted slices,
typed candidate boundary, source rules, and initial vertical-slice acceptance
live at `.data/designs/co-work/truth-surface/ai-assisted-interaction-roadmap.md`.
