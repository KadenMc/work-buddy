# Co-work Truth surface

**Status:** Accepted design and implementation contract for the peer
Provenance boundary and AI-assisted interaction model.

## Decision

Co-work has four first-class rail surfaces:

1. **Review** is the modification inbox for proposals, flags, evaluation results,
   and claims that require a decision.
2. **Provenance** is the place to inspect document source, authorship,
   human-review, attester, and basis records and to make constrained
   append-only review attestations.
3. **Truth** is the place to observe and manage the claim ledger beneath the
   work.
4. **Chat** is the conversational interaction surface.

The peer Provenance contract supersedes this document wherever older text
placed document-authorship treatments inside Truth. See
`../provenance-surface/README.md`. Truth still owns and explains provenance of
Truth-domain records such as candidate preparation, evidence acquisition, and
human claim decisions.

Truth is intentionally both an observability surface and a modification
surface. It is not a renamed Review queue. A person can inspect what the
document rests on without first having a pending task, then begin an appropriate
claim or connection change from the same context.

Truth is also not primarily a manual ledger-authoring form. Its default
mixed-initiative interaction is for AI to prepare exact, typed claim,
expression, and evidence work for human review. Direct claim and connection
authoring remain available as secondary escape hatches for cases in which a
person already knows exactly what should be recorded.

This decision narrows the earlier naming rule that treated truth as entirely
internal. **Truth** is now deliberate product language for this ledger-facing
tab. Low-level terms such as `TruthStore`, store IDs, canonical hashes, gestures,
and database rows remain implementation language rather than routine UI copy.

## Terminology

| Term | Meaning in Co-work |
|---|---|
| **Provenance** | The first-class rail surface for document source, authorship, human review, attester/basis, target health, and append-only review history. |
| **Truth** | The first-class rail surface for observing and managing claims, their document expressions, evidence, decision/acquisition provenance, and lifecycle. |
| **Claim** | A proposition recorded in the append-only Truth ledger. A claim is not automatically a fact. |
| **Fact** | A filter result, not a claim kind: a claim currently held as authoritative. It has confirmed base status, is current-valid, is not redacted or voided, and has no active needs-review overlay. |
| **Expression** | A document passage where a claim is said, with a role such as quote, paraphrase, summary, or instantiation. |
| **Evidence / receipt** | What supports the claim or what the person was shown when making a decision. An expression is not evidence merely because it contains similar words. |
| **Needs review** | An active attention overlay on a claim. It remains separate from the claim's base lifecycle status. |
| **Candidate claim** | A proposition prepared by an analysis run for review. It is not yet a ledger claim or a fact. |
| **Evidence candidate** | A source passage found during research but not yet selected and captured as an immutable evidence receipt. |
| **This document** | Claims connected by expressions to the open document. |
| **Folder** | Claims in the open Co-work folder's one ledger, including claims expressed elsewhere or not connected to a document. |

Use **Truth** for the tab and **Facts** for the authoritative-current filter.
Do not introduce a separate “Facts” subsystem or use `claim_kind == fact` as the
filter definition.

## User jobs and interaction model

Truth directly supports four user jobs:

1. **Discover:** identify the checkable claims expressed by the work.
2. **Ground:** find and assess the evidence which supports, qualifies, or
   contradicts those claims.
3. **Decide:** review prepared claim, expression, and evidence work and decide
   what should enter or change in Truth.
4. **Maintain:** identify what needs renewed attention after prose, evidence,
   or a claim's foundations change.

These jobs, rather than kernel mutation names, govern the primary information
architecture. **Analyze passage** is the first implementation slice. AI uses
the selected passage plus only the permitted surrounding context to select,
disambiguate, and decompose checkable propositions; infer claim kind and
expression role; match likely existing claims; and, when it would materially
help assess a factual claim, perform bounded guarded web research. A run may
issue at most three queries, admit five lead-only hits per query, and fetch only
server-issued hit IDs through the public-network boundary. It returns typed
prepared items for review instead of copying the selection into a blank form.

One run accepts at most 32 KiB of selected UTF-8 text. Its existing-Truth
context is capped at 32 KiB serialized (including at most 200 claims and 200
recorded support receipts under smaller sub-budgets), and its complete worker
context is capped at 90,000 serialized bytes. Normalized output is capped at
80,000 bytes, 20 claim candidates, and 10 evidence candidates per claim. The
account-model worker session has a provider-enforced hard ceiling of $2.00.
Guarded web search and fetch use separate provider-dependent egress and
currently have no enforced monetary ceiling; their query, fetch, time, and byte
limits are not a promise about provider charges.

The later slices are **Analyze document**, **Find evidence**, **Build from
source**, and change-driven maintenance. Their staged plan and acceptance
boundaries live in [ai-assisted-interaction-roadmap.md](ai-assisted-interaction-roadmap.md).
**Find evidence** remains a later dedicated, claim-contextual workflow: it adds
an explicit source boundary, deliberate research steering, broader connected
and Folder sources, and ongoing evidence management. Analyze passage's bounded
research is not that workspace. Each run reports the source classes it
actually searched, failed to search, or did not search. Detecting a citation,
seeing a search snippet, or finding no recorded receipt is not the same as
evaluating an exact fetched source passage.

## Information architecture

The rail tabs are **Review | Provenance | Truth | Chat** on desktop and narrow
layouts. The tablist keeps all four tabs keyboard- and scroll-reachable rather
than clipping one at narrow widths.
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

The primary AI-assisted entry point is **Analyze passage**. Analysis results
open as a Truth review flow whose prepared fields are editable on demand.
**Add claim manually** and **Connect selection manually** remain secondary
actions, grouped under an Add or overflow affordance rather than competing
with Analyze passage. Actions on an existing claim are contextual and
state-valid. Truth must not present an always-visible generic row of lifecycle
verbs.

## Relationship to Review

Review is optimized for “What needs my decision?” Truth is optimized for “What
does this work currently rest on, and how did it get that status?”

Truth owns the complete domain review for prepared claims, inferred
expressions, evidence candidates and receipts, support assessments, conflicts,
and lifecycle decisions. Review remains the generalized attention inbox: it
cross-lists durable claims requiring attention and may carry one compact
handoff when a Truth analysis is ready, but it does not duplicate the complete
Truth analysis or evidence-research workspace.

Only actionable claim states are cross-listed into Review:

- proposed claims;
- challenged claims; and
- claims with an active needs-review overlay.

Routine confirmed facts and terminal history do not clutter Review. Cross-listed
cards and Truth records share the same permanent claim identity, so a decision
made from either legitimate surface refreshes both projections. Truth does not
duplicate edit proposals, Verify setup, or Co-think controls.

Any prose change suggested while grounding or maintaining a claim becomes an
ordinary immutable proposal in Review. Chat integration is a later extension:
the current Analyze-passage slice does not start, steer, or explain an analysis
through Chat. When that extension is built, it must carry the exact target and
must not let conversational assent accept a candidate or confirm a claim.
Co-work Verify remains the criteria-checking subsystem; Truth analysis builds
and maintains the claim-expression-evidence record rather than masquerading as
another Verify check.

The cross-list lives inside Review's existing scroll body. It must not become an
unbounded fixed block above that body that compresses the proposal workspace.

## Editor lens semantics

The editor has an explicit view-only lens state:

- `review`: tracked proposals, flags, and evaluation-result anchors;
- `provenance`: document source, authorship, human-review, and target-health
  treatments;
- `truth`: expression anchors; or
- `neutral`: neither persistent ledger overlay.

The active rail surface controls the default lens: Review selects `review`,
Provenance selects `provenance`, Truth selects `truth`, and Chat selects
`neutral`. Temporary passage highlights from Chat, Working on, or an explicit
navigation command are independent of the lens.

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

Provenance follows the same ownership rule with its own typed provider and
panel projection. It may share the authoritative open-document snapshot source,
but neither Truth nor Review becomes a generic provenance transport. Its editor
persistence barrier and forced-fresh review preflight are specified by the peer
Provenance contract.

All modifications use canonical Truth and Co-work operations. The UI does not
write status rows, expressions, or receipts directly. Human decisions remain
bound to the exact claim and displayed context. For Co-work, that hash-bound
context includes lifecycle and review state, support and premise assessment,
conflicts and derivations, source-integrity state, and prose connections as
well as the canonical claim and receipts. Any change requires a fresh review.
Agents may propose, but cannot approve their own claims or edits.

AI analysis output first lands as a durable, typed **candidate** in operational
run state outside the append-only Truth ledger. A candidate binds the exact
document target and hashes, proposed proposition and kind, expression selector
and inferred role, possible existing-claim match, evidence candidates and
support assessment when available, and explicit ambiguity or limitations. Raw
model prose never becomes a candidate without server-side schema, identity,
anchor, and policy validation.

This staging boundary prevents a mistaken model inference from permanently
minting a claim or expression. It is especially important while expressions
have no safe unlink or retraction lifecycle. Accepting a candidate calls the
canonical operation to create a proposed claim and expression or connect the
passage to an existing claim. Only an exact, separately bound human decision
can make a claim a current fact. Editing any prepared field changes the
canonical payload and requires a fresh decision binding.

Provenance records two separate acts. Candidate output is AI-prepared and bound
to the run, provider/model authorization, and hashes. **Add as proposed** or a
connection consequence is authored by the human who made that decision while
retaining the run and candidate metadata. A fetched source keeps its
`agent_run` acquisition actor and external-quarantined origin; human selection
authors the later support decision without relabelling the acquisition as
human-created.

The persisted run states are `prepared`, `launching`, `running`, `completed`,
`unavailable`, and `failed`; the UI projects the first two as **queued** and
`unavailable` as **failed**. Each run has a thirty-minute execution deadline.
An overdue active run is atomically terminalized as `failed` with
`execution_deadline_exceeded`; even a worker-context read may perform that lazy
terminalization. Candidate dispositions are `pending`, `saved`, and
`dismissed`. Stale is not a persisted candidate status: a human decision
revalidates the exact candidate and current passage, fails with a stale-target
error if either cannot be proven, and leaves the person to rerun analysis.
Worker submission returns only `ok`, receipt schema, run ID, public status, and
`output_sha256`; candidate content remains in the durable run projection and
the receipt does not imply a ledger write.

Analysis and research use the exact captured target. Surrounding document
context may be supplied only when permitted and only for interpretation; it
does not silently widen the expression or evidence span. The UI must disclose
the actual model/provider and source boundary when they imply content egress.
It must never claim that work ran locally unless that is guaranteed by the
executed authorization.

Confirm and Reaffirm must also match the claim's exact lifecycle state inside
that write transaction. Quarantined-only support remains fully observable, but
confirmation is unavailable until a dedicated quarantine-override ceremony is
designed.

Connecting a passage records where a claim is expressed. It does not edit the
prose and does not manufacture evidence. Creating a claim from selected prose
may create a proposed claim and an expression, but the selection is still not
proof of the proposition.

Evidence research follows a truthful source ladder: match existing claims and
follow their recorded receipts; inspect permitted cited artifacts and Folder
sources; query configured connected sources; and use the open web only when
authorized. Analyze passage currently establishes the bounded open-web seam:
search results are lead-only, only a run-admitted hit may be fetched, every
destination and redirect must pass the public-network guard, and captured text
retains exact acquisition and truncation metadata. Fetch allows only port 80
for HTTP and port 443 for HTTPS, at most five redirects, twenty seconds total,
ten seconds per request, and 512 KiB of identity-encoded response bytes. The
model-facing extracted text is capped at 64 KiB; a partial capture records
`text_truncated`, full and captured byte counts, and both full-extracted and
captured-content digests. A claim is not evidence for itself, a citation marker
or search snippet is not a source receipt, source trust is separate from
support, and failure to find evidence is separate from
contradiction. Relationship assessments use
**supports**, **partially supports**, **contradicts**, **does not address**, or
**inconclusive**. Only selected source passages become immutable evidence
receipts; external and adversarial content retains its trust and quarantine
classification.

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

No analysis runs automatically on every keystroke. Deterministic hashes,
expression fingerprints, source integrity, and dependency edges identify
affected work first; a person then starts or authorizes semantic re-evaluation
of the bounded target. AI may prepare several items in one sitting, but there
is no **Confirm all**: each confirmed proposition retains its own exact human
gesture.

## Persistence, states, and accessibility

Persist the selected rail tab, Truth location, active filter, and an independent
Truth scroll position per full folder and document identity. Restoration is
interface continuity, not document state. It must never replay a passage jump.
List scroll is stored separately from transient details and selection forms:
drill-ins begin at the top, returning restores the list, and changing location
or filter resets the new list to the top. The client consumes every backend
page so Folder browsing, connection choices, and Review cross-listing never
silently stop at an arbitrary first-page limit.

Durable analysis progress and prepared candidates survive tab changes,
refresh, and sidecar restart. Reopening a run restores its review position but
does not replay editor navigation. A document or source change is checked when
the person decides: if the server cannot prove that the candidate still applies
to the exact target, the decision fails as stale and the surface asks for a new
analysis. It never silently broadens or rebases the reviewed content.

Initial loading keeps the rail geometry stable. A background refresh retains
the prior list, selection, filters, and scroll position. First-load failure
shows a retry action; refresh failure shows stale data with a non-destructive
warning. Empty states are distinct:

- no document connections: **No claims are connected to this document** with
  **Analyze passage** and a secondary **Add manually** escape hatch;
- no claims in the folder: **No claims in this folder** with **Analyze
  passage**; and
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
7. AI-assisted and manual claim/connection flows preserve human/agent
   authority and never turn selected prose into fabricated evidence.
8. Read-only mode retains complete observation and navigation while explaining
   unavailable writes.
9. Reload restores the tab, location, filter, and independent Truth scroll;
   refresh and SSE do not snap either pane or replay navigation.
10. Loading, first-load error, refresh error, the three empty states, forced
    colors, keyboard navigation, and focus return are covered by automated or
    live acceptance tests.
11. Analyze passage prepares the proposition, claim kind, and expression role;
    those fields do not begin as manual homework or a verbatim-copy default.
12. Ambiguous references cause explicit abstention or a candidate limitation,
    never an invented disambiguation, and a multi-claim passage may produce
    several separately reviewable candidates. A structured clarification flow
    belongs to a later Chat integration.
13. Prepared candidates create no claim, expression, Truth-ledger evidence
    span or support link, or fact until the applicable human action is taken
    through canonical operations. Run-owned external acquisition receipts stay
    outside the ledger until a human selects an exact source passage.
14. Analyze passage truthfully reports completed, failed, partial, and
    unperformed source work from durable receipts. Search snippets, citation
    text, and document expressions never appear as supporting evidence by
    implication.
15. Accepting or editing one prepared item remains bound to the exact captured
    document version, candidate payload, and displayed context; stale work
    cannot be confirmed.
16. Truth owns the full candidate and evidence review, Review cross-lists
    attention without duplicating the workspace, Chat cannot implicitly
    confirm, and Verify remains criteria evaluation.
17. Model and source egress copy describes what actually executed, including
    escalation to an API-backed provider when applicable.

## Extension points

The architecture admits document-scale analysis, source research, source-first
claim extraction, change-driven maintenance, focused challenge, supersede,
expression correction, richer receipt inspection, claim comparison,
multi-user actor selection, and broader redaction subjects. Those workflows
extend the Truth domain surface and canonical operations. They do not turn
Truth into a duplicate of the general Review inbox, overload Chat, collapse
Truth into Verify, or change the claim/expression/evidence distinction.
