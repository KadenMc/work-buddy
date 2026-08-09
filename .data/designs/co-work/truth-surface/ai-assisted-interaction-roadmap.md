# AI-assisted Truth interaction roadmap

**Status:** Accepted interaction-model correction and staged implementation
direction. This document does not claim that the described AI workflows are
implemented.

**Amends:** [README.md](README.md), which remains the canonical Truth-surface
architecture and acceptance contract.

## Why this correction exists

The first Truth surface proved the ledger projection, editor lens, exact
passage navigation, guarded human decisions, and manual claim/expression
mutations. Its primary actions nevertheless exposed the kernel operations as
user work: the person had to copy or rewrite a proposition, choose claim kind,
and classify the expression role. Missing evidence ended in an observation
rather than assistance.

That interaction is a useful expert fallback, but it is not the intended
default for AI-assisted knowledge work. Truth should reduce the effort needed
to discover, ground, decide, and maintain the claims beneath a document while
preserving the human authority and exact-source guarantees already in the
kernel.

The correction is therefore structural:

- AI prepares typed, reviewable work;
- the human confirms, modifies, connects, or declines it;
- candidates remain outside the append-only ledger until accepted;
- claims, expressions, and evidence remain distinct; and
- manual authoring stays available without occupying the primary action slot.

## User jobs

| Job | User question | Product response |
|---|---|---|
| **Discover** | What checkable claims does this passage or document make? | Select, disambiguate, and decompose candidate claims; infer their document expressions. |
| **Ground** | What evidence supports, qualifies, or contradicts each claim? | Search permitted sources, expose exact passages, and assess their relationship without converting retrieval into truth. |
| **Decide** | What should enter or change in Truth? | Present exact prepared items for human acceptance, modification, connection, confirmation, rejection, or abstention. |
| **Maintain** | What needs attention after the work or its foundations change? | Use hashes and dependency state to identify affected items, then re-evaluate only those bounded targets. |

## Surface ownership

| Surface | Owns | Does not own |
|---|---|---|
| **Truth** | Analysis runs; prepared claim, expression, and evidence work; support assessment; claim browsing; complete domain review and lifecycle management. | Tracked prose edits, generic criteria configuration, or a second conversation. |
| **Review** | The general attention inbox. It cross-lists durable proposed, challenged, and needs-review claims, may show a compact “Truth analysis ready” handoff, and owns any resulting prose proposal. | The full claim-decomposition or evidence-research workspace. |
| **Chat** | Later: natural-language initiation, steering, clarification, and explanation with the exact current target carried into the command. The current Analyze-passage slice has no Chat handoff. | Implicit candidate acceptance or claim confirmation through conversational assent. |
| **Verify** | Evaluation of an exact document target against selected criteria, with coordinator-routed results and revision proposals. | Building or maintaining the claim-expression-evidence record. |

The same durable claim identity refreshes Truth and Review after a legitimate
decision. A Review handoff opens the relevant Truth item rather than creating a
second copy of its evidence or lifecycle state.

## Terminology and non-negotiable distinctions

- A **candidate claim** is prepared operational work. It is not a ledger claim
  and cannot appear in Facts.
- A **claim** is the proposition entered in the ledger.
- An **expression** is where document prose says that claim, with a role such
  as quote, paraphrase, summary, or instantiation.
- An **evidence candidate** is a retrieved source passage awaiting selection.
- An **evidence receipt** is an immutable captured source item and span.
- A **support assessment** describes the relationship between evidence and a
  claim. It does not confer fact status.
- A **fact** remains a current authoritative claim under the existing derived
  semantics. AI never promotes a candidate or proposed claim to a fact.

The document expression cannot serve as evidence merely because it contains
the claim. A citation marker identifies a possible source; it is not the source
passage and does not prove support. Source integrity, source trust, and
claim-support relationship remain separate axes.

## Slice 1: Analyze passage

### Job to be done

> When I select a passage whose factual commitments matter, prepare the claims
> and document connections I am likely to want so that I can review them
> instead of manually modeling the ledger.

This is the first implementation slice and the new primary Truth action. It is
small enough to build and test end to end, while establishing the same durable
run and candidate substrate later document-scale and research workflows use.

### Entry and target

- The primary action is **Analyze passage**.
- It is available for one exact non-empty editor selection. A later extension
  may use the current Working-on target without changing the analysis contract.
- Activation captures the selection before rail focus changes and binds the
  exact document identity, structured head, Y.Doc generation, projection hash,
  quote selector, and permitted context boundary.
- Surrounding document context may help resolve references such as “it” or
  “this result,” but it cannot silently widen the expression selector.
- The selected passage is capped at 32 KiB of UTF-8. Existing Truth context is
  capped at 32 KiB serialized, including at most 200 claims and 200 recorded
  support receipts under smaller sub-budgets. The complete worker context is
  capped at 90,000 bytes; normalized output is capped at 80,000 bytes, 20
  claim candidates, and 10 evidence candidates per claim.
- The account-model worker session has a provider-enforced $2.00 hard ceiling.
  Guarded web search and fetch are separate provider-dependent egress and have
  no enforced monetary ceiling; their activity limits do not guarantee cost.

### AI work

The first run performs typed claim analysis rather than returning free-form
advice:

1. **Selection:** determine which content is checkable and which is rhetorical,
   normative, procedural, or otherwise outside the selected claim kinds.
2. **Disambiguation:** resolve references from permitted context; abstain and
   record a limitation when more than one interpretation remains plausible.
   A focused interactive clarification belongs to the later Chat integration.
3. **Decomposition:** prepare one context-independent proposition per atomic
   claim. One selected sentence may produce zero, one, or several candidates.
4. **Classification:** suggest the allowed claim kind and how the exact passage
   expresses the proposition.
5. **Ledger matching:** compare against active Folder claims and identify a
   likely existing match, possible conflict, or genuinely new candidate. Follow
   existing matches to their recorded evidence receipts for display.
6. **Bounded research:** when it would materially help assess a factual claim,
   issue at most three guarded web queries, admit at most five lead-only hits
   per query, and fetch only server-issued hit IDs. Assess exact fetched
   passages rather than search snippets.
7. **Limitations:** identify unresolved citations, ambiguity, unavailable
   context, or missing recorded support explicitly.

Analyze passage does not perform open-ended research. It can inspect matched
recorded receipts and use the run-owned guarded web broker under fixed limits.
Search results remain leads: provider-inline text is discarded, an arbitrary
URL cannot be fetched, every destination and redirect must resolve only to
public addresses, only HTTP port 80 and HTTPS port 443 are accepted, and the
actual socket is pinned to a validated address.
Successful fetches preserve exact bounded text, digest, source URL, title,
redirects, acquisition metadata, and explicit truncation integrity. Each fetch
allows at most five redirects, twenty seconds total, ten seconds per request,
and 512 KiB of identity-encoded response. Model-facing text is capped at 64
KiB; a partial capture records `text_truncated`, full and captured byte counts,
and full-extracted and captured-content digests. The run must report each
source class it searched, failed to search, or did not search
rather than present “no evidence” as a claim about the world.

### Prepared review item

Each server-validated candidate contains at least:

- run and candidate identities;
- exact action-snapshot and target hashes;
- exact expression selector and displayed quote;
- proposed atomic proposition;
- proposed claim kind;
- inferred expression role;
- likely existing claim identity and match classification, when available;
- existing evidence-receipt summaries and exact run-owned fetched-source
  candidates, when available;
- citation cues, ambiguity, and other limitations;
- model/provider authorization and output hashes; and
- current candidate disposition: `pending`, `saved`, or `dismissed`.

Stale and expired are not persisted candidate dispositions. Every human
decision revalidates the candidate hash and exact passage; an unprovable target
fails the decision as stale and asks for a new analysis.

The model may provide structured values, but the server recomputes target
identity and validates every quote/selector against the frozen projection. Raw
reasoning, worker stdout, and unanchored prose do not become domain records.

### Truth review flow

Truth replaces the blank authoring form with a sequence of prepared cards. A
card first shows the proposition, highlighted document expression, suggested
role, existing-claim match, recorded-support status, exact fetched-source
passages, and limitations. Search snippets never occupy the evidence region.
Manual controls appear only after **Edit**.

Available outcomes are state-specific:

- **Add as proposed** for a new candidate;
- **Connect existing claim** for a matched existing claim;
- **Edit** the proposition, kind, or expression role, then submit the amended
  exact payload;
- **Skip** without a ledger write.

A run/candidate-aware **Discuss in Chat** outcome is a later extension, not a
control in the current review card.

Adding a new candidate creates a proposed claim and its expression through the
canonical human dashboard operation. Connecting uses the existing claim and
creates only the accepted expression. Neither outcome confirms a claim. A
separate exact confirmation remains necessary before a proposed claim can
appear in Facts.

The candidate is AI-prepared and retains run, provider/model authorization,
and output hashes. The accepted claim/expression consequence is authored by the
human who chose it while retaining that preparation metadata. A selected web
source retains its `agent_run` acquisition actor and external-quarantined
origin; the human authors the later support decision.

There is no bulk **Confirm all**. A review sitting may mark many prepared
items efficiently, but a later confirmation still binds one exact proposition
and displayed context per human gesture.

### Failure and recovery

- Capture failure leaves the editor selection intact and offers another exact
  capture.
- Ambiguity returns abstention or a candidate limitation, not a guessed
  proposition. Structured clarification awaits the later Chat integration.
- Zero checkable claims is a successful outcome with an explanation, not an
  empty ledger state.
- Invalid model output is rejected with a typed `invalid_output` error and no
  candidate is staged. The worker may correct and resubmit before its deadline;
  raw output never surfaces as a candidate.
- Identical search and admitted-hit fetch retries return their durable receipts
  without another outbound request. An interrupted outbound call whose result
  was not durably observed becomes `research_outcome_unknown`; it is not
  silently replayed.
- Refresh, tab switching, and sidecar restart restore durable run progress and
  the current review item.
- Every run has a thirty-minute execution deadline. An overdue `prepared`,
  `launching`, or `running` run is atomically recorded as `failed` with
  `execution_deadline_exceeded`; a worker-context read may perform that lazy
  terminalization.
- If the document changes, the decision path must prove that the candidate
  still refers to the exact target. If it cannot, the decision fails as stale
  and the person reruns analysis. It never silently broadens or rebases the
  accepted passage.

### Slice-1 acceptance

The first implementation honestly answers the manual-authoring criticism only
when all of the following are true:

1. The user selects prose and receives AI-prepared propositions, kinds, and
   expression roles without first filling those fields.
2. A multi-claim passage can yield several atomic candidates; a non-claim can
   yield none.
3. An ambiguous referent produces abstention or an explicit limitation, not
   invented specificity.
4. Existing claims are matched before a duplicate is proposed.
5. Existing recorded evidence is shown as receipts. Bounded web search and
   admitted-hit fetches are reflected from durable source coverage; unqueried
   classes remain explicitly **Not searched**.
6. The document passage, citation marker, search snippet, and matched claim are
   never relabelled as supporting evidence. Only an exact quote anchored in a
   completed run-owned fetch can become a web evidence candidate.
7. No claim or expression exists before the applicable human acceptance.
8. Acceptance uses the exact frozen target and canonical operation; editing
   invalidates the prior candidate binding.
9. The new claim remains proposed until a separate human confirmation.
10. Analysis jobs and candidates resume durably without replaying passage
    navigation or moving editor/rail scroll unexpectedly.
11. Actual model/provider egress is disclosed truthfully; no generic “runs
    locally” claim survives a permitted API escalation.
12. **Add claim manually** and **Connect selection manually** remain reachable
    but visibly secondary.

## Later slice: Analyze document

### Job to be done

> Show me the important claims across this document or bounded Working-on
> target, what is already connected, and which prepared items need judgment.

This widens the Slice-1 target without changing candidate semantics. It adds
prioritization, paging, resumption, and per-item marks so a long document does
not become an undifferentiated claim dump.

The review order emphasizes ambiguous, conflicting, unconnected, and
unsupported candidates. Routine matches to already-connected claims remain
observable but do not dominate the queue. The default run is explicit and
version-bound; opening Truth or typing a character does not start a paid or
egressing model call.

## Later slice: Find evidence

### Job to be done

> Search the places I permit for exact passages that support, qualify, or
> contradict this claim, then let me inspect and attach the useful ones.

The entry point appears on existing and candidate claim details, including the
current empty-evidence state. The run uses a visible source boundary and a
truthful search ladder:

1. existing claim matches and their recorded receipts;
2. explicitly cited artifacts and permitted Folder files;
3. configured connected sources or registered stores; and
4. open-web search and fetch only under the applicable authorization.

Analyze passage already provides a small fixed web-research loop while
preparing claims. **Find evidence** is a distinct contextual job, not a renamed
toggle for that loop: it starts from one chosen claim, lets the person choose
and steer source classes, searches cited, Folder, connected, and web sources,
supports follow-up queries, and manages evidence after initial claim
preparation.

Retrieval produces evidence candidates. A separate evidence evaluator returns
one of **supports**, **partially supports**, **contradicts**, **does not
address**, or **inconclusive**, with the exact source quote and limitations.
The current general web-search relevance classifier is not sufficient for this
relationship assessment.

The human can select a source passage to capture and attach, refine the claim,
or prepare a supported challenge. Selected passages become immutable evidence
and evidence spans through canonical operations. Unselected search results do
not silently fill the ledger. A source can be captured by an agent and still
remain agent-fetched, external, or quarantined; review does not launder its
provenance.

## Later slice: Build from source

### Job to be done

> Given a source I am reading, prepare its useful assertions and exact source
> spans so I can decide what belongs in this Folder's Truth before or while I
> write.

The first supported inputs should be formats the source boundary can parse and
snapshot reliably, starting with text and Markdown. PDF and Word follow their
established importer/provider seams rather than entering as ad hoc model
attachments.

AI selects and decomposes source assertions, records exact candidate spans,
matches existing claims, and shows relevance to the current document when
available. A source saying something is evidence that the source said it, not
automatic confirmation that the assertion is true. Accepted items become
captured evidence plus proposed claims; the ordinary human confirmation gate
still applies.

## Later slice: Maintain after changes

### Job to be done

> Tell me which claim connections or foundations were affected by changes and
> help me review only those items.

Maintenance begins deterministically. Document hashes, expression
fingerprints, source-integrity state, claim supersession, and dependency edges
identify affected items before any semantic model call. Truth then offers
**Review changes** for the bounded set.

AI may prepare a changed expression role, refined claim, new supporting or
contradicting evidence, challenge, or supersession candidate. It never edits
the document directly. Any proposed prose correction goes to Review as the
ordinary immutable edit proposal. Reaffirmation, challenge, supersession, and
confirmation retain their exact human authority boundaries.

## Durable analysis architecture

### Operational state, not ledger truth

Analysis runs and candidates belong in durable operational state next to the
existing model-job and coordination machinery. The Truth ledger receives only
canonical claims, expressions, selected evidence, lifecycle events, and human
decisions. This keeps model retries, partial output, skipped candidates, and
invalid responses from becoming permanent epistemic records.

The infrastructure may reuse Verify's proven mechanics—exact action snapshots,
model-call authorization receipts, durable queue dispatch, least-authority
workers, typed submission, restart reconciliation, and SSE invalidation—without
reusing Verify's product semantics or dock.

At minimum the operational model needs:

- one immutable analysis run bound to store, document, action snapshot,
  provider/model, context and source policy, and configuration hashes;
- typed prepared candidates with an append-only operational disposition;
- server-generated target and output hashes;
- persisted run states `prepared`, `launching`, `running`, `completed`,
  `unavailable`, and `failed`, with a thirty-minute deadline that maps overdue
  active runs to `failed` / `execution_deadline_exceeded`;
- candidate dispositions `pending`, `saved`, and `dismissed`; stale is an exact
  decision failure rather than a stored disposition, and cancellation is not a
  separate state in this slice;
- idempotent exact retries and conflicting-reuse rejection; and
- a canonical consequence reference after human acceptance.

Typed submission returns only a compact receipt: `ok`, schema
`wb.cowork.truth-analysis-submit-receipt/v1`, `analysis_run_id`, public
`status`, and `output_sha256`. Candidate content remains in the durable run
projection rather than being echoed as a false ledger consequence.

### Source and egress authority

The model authorization and the evidence-search authorization are distinct.
Analyze passage may send the exact target and permitted context to its selected
model. Its worker receives only four run-scoped capabilities: read the frozen
job, bounded search, fetch a server-admitted hit, and submit one typed output.
It has no arbitrary Folder, URL-fetch, ledger-write, or human-decision
authority. Find evidence later receives a visible source allowlist and content
boundary for a deeper claim-contextual workflow.

User-facing copy reports what actually executed:

- provider and model when useful for informed choice;
- whether document text leaves the machine;
- which source classes will be searched;
- whether open-web access is enabled;
- the provider-enforced $2.00 account-model worker-session ceiling; and
- the separate provider-dependent web-research path, which currently has no
  enforced monetary ceiling despite strict activity and network limits.

Do not infer “local” from a worker role, configured preference, or first-choice
provider. If a permitted fallback can use an API-backed model, the UI describes
that possibility before the run.

### Authority and mutation

- AI can analyze, retrieve, capture according to policy, and prepare changes.
- AI cannot confirm or reaffirm claims, mint a human gesture, or approve its
  own prose proposal.
- Candidate acceptance does not imply fact confirmation.
- Every accepted target is revalidated against current exact hashes.
- Every confirmation remains bound to the exact server-composed claim and full
  displayed decision context.
- External evidence preserves acquisition, trust, integrity, author, model,
  and quarantine provenance.
- An expression is not written before human acceptance while no correction or
  retraction lifecycle exists for mistaken expressions.

## UX rules across slices

- Lead with the user's action—Analyze passage, Analyze document, Find evidence,
  Analyze source, Review changes—not schema verbs.
- Keep candidate proposition, kind, and expression role collapsed into a
  readable prepared summary until Edit is requested.
- Show the document expression and source evidence in visually distinct
  regions with explicit labels.
- Prefer exact quotations and source links over model rationales or unexplained
  confidence scores.
- Distinguish supported, partially supported, contradicted, not addressed,
  inconclusive, not searched, and source unavailable.
- Use existing Hover help on the actionable element for conceptual explanation;
  do not add permanent instructional paragraphs or synthetic question-mark
  controls.
- Preserve the independent editor and Truth scroll positions. Selecting a
  prepared item may emphasize its expression; only an explicit **Show in
  document** action scrolls the editor.
- Keep source search and model execution explicit. Later background
  invalidation may identify work for a rerun but must not launch external
  calls; the current slice surfaces stale only when an exact decision cannot be
  revalidated.
- Provide efficient per-item keyboard decisions without a semantically false
  bulk confirmation.

## Validation strategy

The evaluation corpus should include at least:

- one sentence containing two independently checkable claims;
- one ambiguous pronoun requiring surrounding context and one which remains
  genuinely ambiguous;
- a rhetorical or normative passage with no factual candidate;
- an exact duplicate and a paraphrase of an existing claim;
- a citation marker with no captured source;
- a matching claim with existing usable evidence;
- supporting, partially supporting, contradicting, irrelevant, and
  inconclusive source passages;
- an external source containing prompt injection;
- a document edit between analysis and acceptance; and
- restart during each operational job stage.

Measure candidate precision and recall separately from human correction rate,
duplicate avoidance, evidence-source opening, time per accepted candidate,
abstention quality, stale-target behavior, and review abandonment. Do not
collapse the pipeline into one opaque “accuracy” number: decomposition,
retrieval, and evidence relationship assessment fail differently.

## Implementation order

1. Durable run and candidate contracts with exact target and provider
   authorization.
2. Analyze-passage worker, server validation, and existing-claim/evidence
   matching, plus bounded guarded web search and admitted-hit fetch receipts.
3. Truth prepared-review UI and canonical Add as proposed / Connect to claim
   outcomes; demote manual actions.
4. Restart, stale-target, accessibility, egress-copy, and live acceptance
   validation for Slice 1.
5. Widen the target and review throughput for Analyze document.
6. Add the broader source-policy, steering, and maintenance contracts for the
   dedicated Find evidence workflow.
7. Reuse the source pipeline for Build from source.
8. Add deterministic invalidation and bounded semantic maintenance.

Each step must leave a user-testable vertical slice. Document-scale analysis
does not begin until one passage can travel from exact capture through typed AI
preparation and human-reviewed canonical consequence without manual schema
authoring or epistemic boundary violations.
