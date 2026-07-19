# Co-work Surface: PRD (v0.2)

**Status: draft for red-pen, agent-authored, updated 2026-07-17 across three resolution rounds. Ratified: the K-phase renumbering, structured-canonical-with-Markdown-feel (the contrast was block-based WYSIWYG, now rejected), one consolidated view, Word-review-mode as the UX anchor, Hocuspocus for the collab phase, and the naming scheme: the umbrella is **work-buddy Co-work** ("Co-work" in prose/UI, `cowork` in code), the view is Co-work, registered documents are **cowork docs**, surface capabilities are `cowork_doc_*`, and Co-think narrows to the future provocation/Socratic MODE within Co-work (binding record: [../../terminology/cowork-scheme.md](../../terminology/cowork-scheme.md)). Still pending Kaden's nod: the two engine/schema deviations in §9. This PRD amends [../tms-glue.md](../tms-glue.md) §II.7 and [../implementation-dag.md](../implementation-dag.md) as described in §2. Architecture and OSS composition live in [architecture.md](architecture.md).**

Sources: the committed kernel plan (tms-glue Part II, shipped K0/K1), the Old-PRD distillation ([old-prd-distilled.md](old-prd-distilled.md)), the AOV digest (`../corpus/aov.md`), the co-think digest (`../corpus/co-think-research-digest.md`), Kaden's notes (`human-notes.md`, `../corpus/kaden-notes-2026-07-11.md`), and Kaden's 2026-07-17 direction and answers.

## 1. Thesis and north star

The surface exists to make AI-assisted knowledge work **reviewable before it becomes believable**, and through that, to restore something specific [Kaden]: *"feeling confident about their knowledge outputs even when AI has been involved in the thinking/writing of it."* The deeper motivation is cognitive protection and artifact faithfulness: keeping artifacts faithful to the user's genuine mental models, beliefs, and preferences, with directable AI collaboration as the on-ramp rather than the point [Kaden, distilled D14].

North star: *"I can let AI deeply participate in my thinking and writing without losing cognitive ownership of the work or corrupting the truth-status of the artifact."*

The confidence comes from structure, not vibes: facts underpin the document, the document projects to Markdown, every durable change passed a human gesture on the exact content, and the reader can interrogate any sentence for what is underneath it. The co-thinking behaviors that ACT on this substrate (provocations, Socratic dispatch, cognitive forcing functions, calibrated friction) are deliberately out of this phase and arrive with co-think.

## 2. Where it sits: the phasing amendment (RATIFIED, with one demotion)

Ratified by Kaden 2026-07-17:

| Phase | Was | Becomes |
|---|---|---|
| K2 | first real stores + consumer onboarding | **this surface** (the Co-work view in the React dashboard) |
| K3 | React review queue + staleness | the old K2 (stores, profiles, `wbuddy project init`, materialize, reconcile), whose reconcile proposals then land in an already-existing surface |
| K4, K5 | verification stack, federation + doc view | unchanged in content. Old K3's backend machinery (sweeps, dedup prompt, refutation screen, redlines diffs) rides along, scheduled with K3 or K4 |

**Demotion (per Kaden's Q1 answer): the old K3 queue UI design and the K5 wave-5B document-truth-view design are INPUT MATERIAL, not canon.** Kaden did not closely review those designs and does not pre-endorse their UX. What is settled: one consolidated view, document editing at its center. Whether and how a claim-queue panel appears inside that view is open UX design (§7), to be settled by mockups, not inherited from the old component inventory. The doc-view's JOB (a document with its truth state legible in context) is inherently this surface's job, but its specific four-treatment design is a reference only.

Why surface-first is sound: K1 already shipped everything the surface needs to be real (stores, claims, gestures, capabilities, events). Reviewing is the kernel's current bottleneck. And the editor is the highest-risk, highest-information build in the plan, so it should meet reality earliest.

## 3. Jobs to be done (v1)

1. **Review agent-proposed tracked edits on real documents.** The AOV Word loop relocated into the dashboard: an agent proposes edits and flags, the human walks them in context as tracked changes with margin cards, marks each one, marks mint per-item gestures, accepted edits apply and materialize. No Word round-trip.
2. **Review proposed claims.** The kernel's pending-claim review (the six claim verbs) available in the same view. UI form open (§7): panel, filter, or mode, decided by mockups.
3. **Write.** A calm, fast, Markdown-feeling editor for registered documents, usable with zero AI involvement. Writing stays pleasant, the surface is a writing surface first and an audit tool second.
4. **Interrogate.** Click a sentence, see what is underneath it: the facts it expresses, their status, their evidence [Kaden]. Read-only in v1, and only where fact links exist, but the seam is first-class from day one.
5. **Give feedback, get help.** Highlight any span and write freeform feedback [Kaden, 2026-07-17]. The feedback is saved verbatim (user-authored evidence anchored to that span, §5) and dispatched to a document-bound agent working in the background. The agent replies in plain, layman-friendly language through a Chat tab (the established dashboard helper-chat pattern), and any edits it makes in response arrive as normal tracked-change proposals through the gate. The same channel serves clarifying questions ("why does this paragraph say X?").
6. **Be the co-think foundation.** CRDT substrate bound from day one, proposals and gestures ledger-canonical, provenance event shape reserved, dispatch/provocation affordances designed-for but not built.

Explicit v1 NON-goals (pared-down directive, supersedes the conversation MVP's wider list): provocations, Socratic dispatch, design friction, verification models (K4), AI autocomplete, in-editor AI generation commands, live multi-client collab and presence, comment threads, mobile editing, C2PA export, session board, progressive context index.

## 4. Hard invariants

Tags: [Kaden] stated or ratified by Kaden, [kernel] shipped or committed kernel law, [AOV] ported AOV semantics, [new] proposed here.

- **I1. Jank is not an option.** Measurable performance targets (§11). [Kaden]
- **I2. The canonical document is richer than Markdown, materializes to clean Markdown, and edits like a continuous Markdown document, NOT a block-based WYSIWYG.** Notion-style block editing is rejected (the "as opposed to" in Kaden's direction). Markdown-canonical is also rejected. The Y.Doc binary (updates + snapshots) is primary storage for registered documents. [Kaden, ratified 2026-07-17]
- **I3. No paid dependencies, ever.** Tiptap Pro, AI Toolkit, Comments, Collaboration cloud are reference designs only. work-buddy stays free. [Kaden]
- **I4. Collaboration, when it arrives, is CRDT (Yjs), not central-authority rebasing.** Hocuspocus is the ratified backend. [Kaden]
- **I5. No durable agent edit and no canon without a human gesture on the exact change.** THE GATE RULE, generalized. [kernel, AOV]
- **I6. Gestures are per-item, hash-bound, single-use, server-composed, and fresh.** Stale-view marks rejected. Confirm-all is structurally invalid. [kernel]
- **I7. Agents cannot approve or confirm their own content, structurally.** [kernel, AOV]
- **I8. Silence is abstain. An ambiguous mark is clarified, never guessed. Nothing is inferred from scroll, dwell, or non-action.** [AOV]
- **I9. AI edits never silently mutate a registered document.** They enter as reviewable proposals through the capability surface. Agents get semantic proposal verbs, never raw CRDT operations, never direct file writes to registered documents. [Kaden principle, kernel enforcement]
- **I10. Anti-anchoring: rejected proposal content does not linger.** Plain rejections redact per policy. Reason-classed rejections convert the decision into durable knowledge (confirmed negations, preference claims). [kernel]
- **I11. Provenance is honest and survives acceptance.** Accepted AI text remains attributable to its producing run AND the human approval event. Human edits to AI text are recorded as human modifications of AI-origin content. Producer identity laws apply to every agent write. [Kaden, kernel]
- **I12. The ledger is the truth about review state. In-document marks and files are projections.** A dropped mark re-anchors from the ledger by quote. Markdown never carries provenance. [kernel, distilled D7]
- **I13. Materialized files stay native, clean Markdown in their natural locations.** Front-matter stamp only. Out-of-band file edits are legitimate input, detected and reconciled through review, never overwritten and never silently absorbed. [kernel]
- **I14. Exactly one tracked-change engine, one history stack, one Markdown serializer.** [distilled gotchas]
- **I15. One store boundary per scope.** Documents, spans, expressions, proposals, gestures, and claims for a scope live in that scope's `.wb-truth/` sidecar. No parallel surface database. [kernel, Q1]
- **I16. Local-first.** Offline, restart, and interruption tolerant. [Kaden]
- **I17. Multi-user product scoping.** Kaden's setup is the validation fixture, not the requirements boundary. [feedback rule]
- **I18. Dashboard citizenship.** Registered App contribution, Theme Contract v1, accessibility obligations, dirty-state guarding, same-origin `/api/truth/*`. [t-af909c0d, carve-out in §9]

## 5. The document model (and the honest state of the kernel)

**What the kernel handles today (K0/K1 shipped): facts, not documents.** The shipped schema has claims, evidence, evidence SPANS on the support side ("this evidence span is how we KNOW the claim"), links, derivations, gestures. There are NO document tables in the shipped DDL. The document side was designed (tms-glue §II.4: two span relations, three document classes) and deliberately deferred with reserved names (`documents`, `document_spans`, `expressions`) and an additive migration path, confirmed safe to defer by the second devils-advocate round. So the truth layer as implemented cannot yet "hack this," and was built precisely so this phase can add it without straining anything: new tables, new profile, no changes to shipped invariants.

**The two span relations, restated** (they answer "facts alone vs documents"):

| | Support (shipped) | Expression (this phase adds, minimal) |
|---|---|---|
| Reads as | evidence span is how we KNOW the claim | document passage is where we SAY the claim |
| Direction | evidence span -> claim | document span -> claim |
| Staleness | source retraction (rare) | claim superseded, or passage edited away |

**The factorized store under the document** [Kaden]: expressions are exactly the "facts operating underneath a sentence" mechanism. Click a passage, follow its expression rows to claims, see status and evidence. Two ends of a spectrum share the machinery: class-1 generated documents (canon files, resumes) are FULLY factorized (every passage maps to a claim via the render manifest, the facts produce the document). Class-2 co-authored documents (what this surface manages) are fact-UNDERPINNED: human-owned prose in which some passages express claims. The editor makes the underpinning legible and growable.

**Registration.** A document becomes surface-managed (a **cowork doc**, the everyday noun) by explicit registration into a scope's truth store: `documents` row (uuid, path, title, profile), current file imported as the initial structured document, import event recorded, hash stored. Idempotent, reversible (retiring re-materializes a final clean file, history retained).

**Canonical object.** Per I2: the registered document IS the structured object, stored as Y.Doc update log + snapshots under `.wb-truth/runtime/` (gitignored binary, compacted), with metadata, spans, expressions, proposals, and events in `store.db`. The `.md` file is the materialized projection, hash-recorded, drift-guarded. Serialization-runtime decision (audit A1): the one serializer is JavaScript, so in v1 import and materialization run in the dashboard client, the server verifies hashes and performs the writes through the engine. Flows needing (de)serialization require the view open in v1. Escalation path: a small Node helper, at latest with the collab-phase Node service.

**Expressions in v1 (scoped small).** Schema lands in full (it is already designed), but creation paths and UI stay minimal:
- An agent proposal MAY carry claim references. Accepting it mints expression rows for the affected spans (the edit "says" those claims).
- An explicit capability + UI action links an existing passage to an existing claim ("this sentence expresses claim X").
- Read path: passage chips and the click-a-sentence inspector (job 4).
Expression VERIFICATION (does the claim actually entail the passage) is K4's NLI machinery, not this phase. Automated expression extraction is co-think's.

**Identity namespaces** (distinct, never collapsed [distilled D12]): `document_id`, `node_id` (Tiptap UniqueID), `span_id` (Web Annotation selector + quote + hash, kernel `anchors.py`), `expression_id`, `proposal_id`, `op_id`, plus kernel claim/evidence/gesture ids. Cross-store references via `wb-truth://` URIs.

**Feedback capture** [Kaden, 2026-07-17]. Span-anchored human feedback is stored VERBATIM as kernel evidence: an `evidence` row (kind utterance, trust class `user_authored`, locator naming the document, span, and conversation) plus a document-span anchor. That makes the user's exact words permanently citable: later claim or preference proposals can cite the feedback as their supporting span, which is how "saved as a kind of fact" becomes literal without pretending an utterance is itself a confirmed claim. Each piece of feedback also lands in the document's conversation thread as the prompt the background agent works from.

**Drift.** v1: last-materialized hash recorded, differing file hash blocks silent regeneration, shows a redlines diff, offers re-import (out-of-band edits enter as an unattested change set for review). Full claim-mapped reconcile arrives with K3 and plugs into the same flow.

**Storage layout** (extends the kernel sidecar):

```
<scope-root>/.wb-truth/
  store.db          # + documents, document_spans, expressions, proposals,
                    #   doc events (additive DDL, ops gated by a profile
                    #   policy block: tables exist in every v2 store)
  store.yaml        # profile gains the document-surface policy block
  runtime/          # Y.Doc update log per document (gitignored, LOCAL-ONLY
                    #   working state, rebuildable)
  export/           # claims.jsonl format v3: document-side ledger records
                    #   join the deterministic export, co-landed with the DDL
<natural location>/<doc>.md   # materialized projection, clean
```

**Durability decision (foundation-audit F1, CRITICAL, resolved here for red-pen).** Backups deliberately carry only `store.yaml` + `claims.jsonl` per store, so the export is the SOLE off-machine durability path. As previously written ("claims.jsonl unchanged", `runtime/` gitignored) a cowork doc's canonical object and its entire proposal ledger had NO backup or repo-travel form. Amended contract: (1) all document-side LEDGER records (documents, spans, expressions, proposals, doc events, their gestures) enter the export at `format_version` 3, in the same change that creates the tables, never after. (2) Each document's latest compacted Y.Doc snapshot is exported as a content-addressed blob alongside evidence blobs, so recovery restores the exact structured document. (3) The update LOG stays local-only working state: fine-grained keystroke history is rebuildable-or-droppable, and its loss on machine death is documented and acceptable (the ledger keeps every decision, the snapshot keeps the document).

## 6. The review model

**Proposal lifecycle** (AOV semantics on kernel tables): `open -> decided (per gesture) -> applied | closed | expired`, append-only status events. Identity for dedup and suppression: `(document_id, normalized quote, replacement hash)`. A proposal whose anchor no longer locates uniquely **expires toward re-review, never toward acceptance** [AOV]. Proposals against a stale base hash cannot mint or refresh anything [AOV stale gate]. An `endorse` on a flag never auto-applies: it routes to the proposing agent, and the drafted fix returns as a NEW linked proposal (`fixes` ref) needing its own accept [AOV gate rule].

**Gesture vocabulary.** On a tracked change:

| Verb | Meaning | Kernel gesture | Durable consequence |
|---|---|---|---|
| Accept | apply exactly this change | `confirm` | edit applies and materializes, proposal `applied`, AI origin + approval event both retained |
| Amend | tweak it, then accept my version | `edit_confirm` | modified content applies, recorded as human modification of AI-origin content |
| Reject | no, and no truth stance | `reject_plain` (shipped kind) | proposal closed, content redacted per policy, suppression remembered |
| Reject as false | the assertion is false | `reject_as_false` (shipped kind) | mints a confirmed negation claim + `refutes` link |
| Reject as preference | violates how I want things | `reject_as_preference` (shipped kind) | offers a preference claim mint |
| Redirect | yes-but, with guidance | `redirect` [ratified, new kind] | typed note routed to the proposing agent, proposal stays open pending redraft |
| Defer | not now | `defer` | stays open, parked |
| (silence) | abstain | none | nothing happens, ever |

On a flag (no replacement): **Endorse** (problem is real, draft me an edit) [ratified, new kind], **Dismiss**, **Redirect**. On a claim card: the six committed claim verbs. The shipped kind vocabulary is richer than this table's column (ten kinds enforced in code, including quarantine override and reaffirm-after-challenge): the table maps VERBS to the kinds they mint, it does not enumerate the enum.

**What makes the gestures AI-native:** every decision is a durable, structured, agent-queryable ledger row with a reason class (agents can ask what the human decided and why, and calibrate). Rejections carry epistemics (reject-as-false creates citable truth that screens future re-proposals). Redirect is first-class typed guidance bound to the exact proposal. Suppression memory prevents re-nagging. Nothing is a bearer token: every gesture binds to the exact content hash it was shown, once.

**Feedback is authored content, not a gesture.** A gesture is a decision on exact shown content. Feedback is the human AUTHORING new content (an utterance), so it is captured as evidence, not minted as a gesture. The two meet downstream: whatever the agent does with the feedback comes back as proposals, and those get gestures like everything else.

**Batch sittings.** Throughput comes from the reading-and-marking flow: walk items with the keyboard, mark each, submit the sitting. Submission mints one gesture per marked item with per-item displayed hashes (kernel fold rule). Dirty state is first-class: route-change guard, localStorage draft retention.

## 7. UX

**Name: "Co-work"** [Kaden, ratified 2026-07-17]. The view, and the umbrella above it, per the terminology record. "Knowledge work" is retired.

**The anchor** [Kaden]: **Microsoft Word's reviewing mode.** Tracked-changes markup over the text itself, comment-like cards in a margin sidebar aligned to their anchors. This is the basis of everything v1 renders.

**The right panel has two tabs** [Kaden, 2026-07-17]: **Review** (proposal cards, flags, claim review, the mark bar) and **Chat** (the document's conversation with its background agent). Chat reuses the house machinery wholesale: the dashboard chat sidebar surface and the `conversation_*` capabilities (`conversation_create` opens the sidebar, the agent sends with `conversation_send`/`conversation_ask`), exactly the "Help me create a job" pattern, bound to one conversation per document. The agent behind it reasons and executes out of view and speaks plainly in the tab. Highlight-and-give-feedback routes into that conversation, and resulting edits surface in Review. The remaining open design matter is how the Review tab is organized internally (grouping, filtering, ordering, where claim review lives), settled by mockups before the contract freeze, not inherited from the old queue-component inventory.

**Visible trust state, including color.** The markup distinguishes, at minimum, three states in v1, all derivable from the ledger without per-keystroke provenance:

1. **Human-written** (default surface, no decoration).
2. **AI-written, human-confirmed** (applied proposals: we know exactly which ranges were AI-authored and approved, and by whom).
3. **AI-proposed, unconfirmed** (open proposals rendered as tracked changes).

Rendered with theme-contract tokens, color plus a non-color encoding (a11y rule). Richer states (source-backed, unsupported, contested, stale, mixed-after-human-edit) layer on later as expressions and verification arrive. Span accuracy under later edits degrades gracefully: re-anchor by quote, and honesty demotes a span to mixed rather than pretending precision.

**Review walkthrough.** Agent proposes via MCP. SSE nudges the view. Open the doc: insertions/deletions inline as tracked changes, flags and rationale as margin cards. Keyboard walks item to item (j/k, Kaden's inverted binding, as a configurable personal binding). One verb per item from the mark bar. Submit the sitting: server validates hashes, mints gestures, applies accepted edits through the engine, re-materializes, returns per-item results. Redirects and endorses notify the owning agent session.

**Interrogate walkthrough.** Select or click a passage: the inspector shows its expressions (claims underneath, with status chips), its provenance (who wrote it, who approved it), and open items anchored there. Read-only in v1.

**Feedback walkthrough.** Highlight a span, choose Give feedback, type freely. The feedback is captured verbatim as anchored evidence, posted into the document conversation, and the Chat tab shows the agent picking it up. The agent may answer in plain language, propose edits (which appear as tracked changes in Review, anchored where the feedback pointed), or ask a clarifying question back. Nothing the agent does lands durably without gestures.

**Writing walkthrough.** Open a registered doc, type. Markdown input rules give the Markdown feel (`#` heading, `**bold**`, lists). Slash menu (cmdk) inserts structure. No block handles, no block drag UI (I2). Human edits are direct, recorded as human-origin, re-materialized on save. Undo is Yjs undo, spiked against annotation survival.

**States.** Empty/loading/error per the dashboard host-state contract. Read-only mode when the store or engine is unavailable, never a fabricated editable state.

## 8. Data and API surface (sketch, frozen at contract-freeze)

Additive store DDL: `documents`, `document_spans`, `expressions`, proposal records + status events, coarse document events, Y.Doc persistence (runtime files + snapshot index rows). Append-only discipline, gated by a document-surface profile.

Routes (same-origin, dashboard service): document list/get (content + open proposals + expressions + hashes), Yjs update push/pull, marks submission (per-item displayed hashes, per-item gestures), materialize/drift/diff, re-import. SSE via existing `/api/events` (`truth.*` plus new `truth.doc_*` events).

MCP capabilities (Co-work-owned, calling into the truth engine): `cowork_doc_list`, `cowork_doc_get`, `cowork_doc_propose_edit` (quote-anchored hunks + rationale + tldr + optional claim refs), `cowork_doc_comment` (flag), `cowork_doc_expression_mark` (link passage to claim, propose-weight). Proposals are normal-weight. All decisions remain human-surface only.

Feedback and chat ride house machinery: a `cowork_doc_feedback` route captures the highlighted span + verbatim text as anchored evidence and posts it into the document's conversation (`conversation_*` capabilities + the dashboard chat sidebar). The document-bound agent is an ordinary work-buddy agent session holding the `cowork_doc_*` surface, so its edits pass the same gates as anyone's.

**Contract-freeze notes from the foundation audit (binding):** sittings live on the dashboard HTTP surface EXCLUSIVELY (the per-invocation MCP decision ops bind one approval to one invocation, so a 30-mark sitting cannot ride them). Dashboard routes call the ENGINE LIBRARY directly (the CLI's pattern), never wrapping the MCP decision ops, whose per-invocation branch would still prompt inside a button click. The sitting path threads a real user identity into gesture actor refs rather than cloning the MCP path's fixed single-user constant (I17).

## 9. Dashboard integration and declared deviations

The view enters as a `wb.cowork` App contribution: lazy route + view module, COMPONENTS.md inventory, Theme Contract v1 tokens, axe coverage, desktop-first (mobile read/review later, editing desktop-only in v1).

Deviations:

1. **New runtime dependencies. RATIFIED** [Kaden]: exact-pinned MIT/Apache deps for the editor contribution (Tiptap, Yjs, cmdk, the forked suggestion engine), lazy-loaded, license audit in the phase gate.
2. **Gesture kinds `redirect` and `endorse`. RATIFIED** [Kaden, 2026-07-17], mechanism corrected by the foundation audit (F5): the shipped enum is TEN kinds enforced by a code-level frozenset (there is no plain `reject` kind, rejection shipped as three reason-classed kinds), with no DB CHECK and no migration involved. Adding the two values is a small code change (S). The redirect/endorse ROUTING (typed note to the owning agent session, drafted fix returning as a linked proposal) is net-new flow logic (M).
3. **Document-side extension bundle. RATIFIED** [Kaden, 2026-07-17, "Approved! Please proceed!"], audit-shaped. The naive form ("additive DDL, profile-gated, touch nothing") was REFUTED by the foundation audit: the invariants hold, but five shipped modules must be extended at deliberately closed extension points. The full bundle, sized: (a) the v2 DDL for `documents`, `document_spans`, `expressions`, proposals, doc events, each with its own append-only triggers (additive, the migration framework is proven for exactly this). (b) Export format v3 + the §5 durability decision, co-landed with the DDL, never after (M, the audit's one CRITICAL). (c) Lifecycle extension: proposals as gesture subjects + the two new kinds + allowed-kind sets (M). (d) Redaction subjects gain proposals, for anti-anchoring on rejected edit content (S). (e) `wb-truth://` URI kinds + `truth.*` event types extended (S, decide-once naming). (f) "Profile-gated tables" restated honestly: tables exist in every v2 store, document OPS check a profile policy block before acting (S-M). Expressions stay scoped per §5.
4. **Renumbering. RATIFIED** per §2, with the queue-UI/doc-view demotion.

## 10. Phasing within the phase (S-waves)

The parallelized execution layer over these waves (orchestrator/verifier model, Opus builders, owned paths, contract freeze, joins, PR landings) is [k2-execution-dag.md](k2-execution-dag.md).

- **S0 spike suite** (isolated route or spike repo, throwaway. Input doc: [tiptap-docs-audit.md](tiptap-docs-audit.md), whose A-items are the checklist): tracked-change engine bake-off (suggest-changes primary vs tiptap-track-changes vs suggestion-mode: agent-proposal ingestion, overlapping edits, accept/reject correctness, Yjs-undo survival of marks including undo-after-accept, edits inside code blocks and on atom nodes, diff layer ignoring `node_id` + provenance attrs), the wb mark flag matrix (`inclusive: false` etc.) verified against real typing at span edges, paste-forgery defense (wb marks non-reconstructible from clipboard or imported HTML), `dispatchTransaction` layering (Tiptap v3 middleware vs editorProps decorator), `@tiptap/markdown` fidelity on a real-doc corpus with block-splice, `contentType: 'markdown'` threading at one ingest boundary, UniqueID + Collaboration init order, React 19 StrictMode double-mount with persistence attached, perf probe at 10k/50k words with dense decorations, `tiptap-apcore` verdict, and low-fi mockups of the review-mode sidebar for Kaden. **Gate: engine chosen, fidelity and perf numbers accepted, sidebar direction picked.**
- **S1 document substrate:** registration, import, Y.Doc persistence + compaction, materializer + block-splice fidelity, drift guard + diff, direct writing UX, contribution scaffolding.
- **S2 review loop:** MCP propose surface, proposal ledger + suggestion rendering, margin cards, mark bar + sittings, per-item gestures + apply path, expressions read path + inspector, SSE, redirect/endorse routing, span-anchored feedback capture + the document conversation (Chat tab via the house chat sidebar).
- **S3 claim review + polish:** claim review in the view (form per the S0 mockup decision), slash menu, keyboard completeness, dirty-guard verification, docs units via `/wb-dev-document`.

**Exit judgment** [Kaden, replacing the formal gate]: no staged throughput ceremony. Once a working end-to-end slice exists (agent proposes, human reviews and decides, edits apply and materialize, ledger legible), Kaden judges the truth implementation AND the review surface together, holistically. Suggested first content, non-binding: a store at the `.data/designs/` scope with this design's own documents registered, so the red-pen of this PRD can happen inside the surface it specifies.

Then K3 (old K2) onward per the amended DAG. Collab (Hocuspocus + y-tiptap + presence), autocomplete, in-editor dispatch, and everything cognitive ride the co-think phasing on this substrate.

## 11. Performance requirements (proposed numbers, red-pen)

On the reference machine (Kaden's G14), measured in S0 and re-checked at the working slice:

- Keystroke to paint p95 under 16 ms at 10k words with 300 live decorations, under 33 ms at 50k words.
- Document open (snapshot + updates + render) under 1 s at 50k words.
- Accept/reject application under 100 ms per item, sitting submission under 2 s for 30 marks.
- Materialization under 500 ms at 10k words.
- No unbounded memory growth across a 2 h session (update-log compaction works).

## 12. Success criteria

1. The working end-to-end slice exists and Kaden's holistic judgment of truth + surface is favorable (§10).
2. Projection fidelity suite green: 20+ real repo/vault docs import and materialize stably, unedited regions byte-preserved, unsupported syntax preserved verbatim and flagged.
3. Gate integrity proven live: N marks mint N gestures with N distinct hashes, stale-view marks rejected, agent self-confirm rejected, silence changes nothing.
4. A full AOV-shaped loop runs end to end: propose, review, redirect once, endorse once, accept, materialize, agent reads back its decisions.
5. Click-a-sentence works on at least one document with real expression rows underneath.
6. The feedback loop runs end to end: highlight, feedback, plain-language agent reply in Chat, a resulting proposal in Review, decided by gesture.
7. Zero paid or non-OSS dependencies, all new deps exact-pinned and lazy-loaded.
8. Perf targets met (§11).

## 13. Risks

| Risk | Mitigation |
|---|---|
| `@tiptap/markdown` is young (docs tag it beta), projection churns diffs | fidelity suite as a gate, block-splice materialization (whole-doc serialization provably normalizes untouched regions, audit A10), exact pin, flagged-not-normalized unknowns |
| Pasted or imported HTML forges provenance/suggestion marks (audit A3) | wb marks are never reconstructible from `parseHTML`, display state re-derives from the ledger (I12), S0 test case |
| Serializer is JS-only, server has no runtime for it (audit A1) | v1 serializes in the client, server verifies + writes via engine, Node helper as sanctioned escalation |
| Tracked-change engines all disappoint on overlap/undo | fallback is custom on `prosemirror-changeset` + the official tracking example, budgeted in S0. Adapter seam contains the blast radius |
| Yjs undo breaks mark/decoration positions | named S0 spike. If unfixable, scope undo semantics explicitly |
| Sidebar UX misses (the Word anchor is a feel, not a spec) | S0 mockups before contract freeze, Kaden picks the direction |
| Expressions expand v1 schema surface | schema is pre-designed, UI read-only, verification deferred to K4, extraction deferred to co-think |
| Scope creep toward co-think | §3 non-goals and the pared-down directive are binding |
| Dual-truth creep (file vs object) | I2/I13 + drift guard from day one, reconcile lands in K3 |
| Node runtime (collab phase) | ratified: Hocuspocus as a sidecar-managed service when collab starts |

## 14. Open items

1. RESOLVED: §9 deviation 3 ratified with the execution approval (2026-07-17). The foundation audit at [foundation-audit.md](foundation-audit.md) is the evidence base, and deviation 3 is the amendment list. Execution is live per [k2-execution-dag.md](k2-execution-dag.md).
2. **Review-tab organization** (where claim review lives, grouping, filtering): S0 mockups, Kaden picks. The panel-level split is settled: Review and Chat tabs.
3. **Naming leftovers:** the document-surface profile name (contract freeze), and the documentation-subsystem rename away from "knowledge" (Opus study in flight at `../../terminology/knowledge-rename-study.md`). The umbrella, view, doc noun, and capability families are settled per the terminology record. Word-by-word approval on any user-facing copy.
4. **Perf numbers** (§11): accept or adjust at red-pen.

---

*Provenance: agent-authored 2026-07-17 by the review-surface design session, revised same day across three rounds: Kaden's first answer round (renumbering ratified with queue-UI demotion, block-based WYSIWYG rejected, Word-review-mode anchor, expressions pulled forward, formal exit gate replaced by holistic judgment, Hocuspocus ratified), the feedback-loop direction (highlight-to-feedback as anchored evidence, Review + Chat tabs on the house conversations machinery), and the Co-work naming ratification (umbrella + view + cowork docs + `cowork_doc_*`, Co-think narrowed to the mode, terminology record at ../../terminology/cowork-scheme.md). Zero em-dashes and zero prose semicolons by rule. Unreviewed, not canon until red-penned.*
