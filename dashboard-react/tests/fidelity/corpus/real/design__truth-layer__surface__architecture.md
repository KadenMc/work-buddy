# Co-work Surface: OSS Composition and Minimum Viable Architecture

**Status: proposed, unreviewed. Companion to [PRD.md](PRD.md), which owns requirements and invariants. This document owns the OSS survey, integration modes, the canonical-truth map, and the glue inventory. Component claims re-verified 2026-07-17 unless noted. Incorporates the Old-PRD conversation decisions via [old-prd-distilled.md](old-prd-distilled.md), cited as [distilled D-n], and the official-docs audit via [tiptap-docs-audit.md](tiptap-docs-audit.md), cited as [audit A-n/B/C-n/D].**

Integration-mode legend (house convention, from tms-glue §II.3): **1** upstream dependency, **2** VCS-pinned, **3** tracking fork, **4** hard fork, **5** vendored source, **port** first-party reimplementation from their design, **lift** design/pattern only, **house** existing work-buddy machinery.

## 1. The architecture in one view

```
┌─ dashboard-react (Vite, lazy contribution) ─────────────────────────┐
│  wb.cowork App: Co-work view                                        │
│  ┌─ Editor pane ────────────────┐  ┌─ Review rail ────────────────┐ │
│  │ Tiptap (ProseMirror)         │  │ proposal cards, flags,       │ │
│  │ + @tiptap/markdown (feel +   │  │ claim chips, mark bar,       │ │
│  │   projection, never canon)   │  │ claim queue                  │ │
│  │ + WbTrackedChangesAdapter    │  │ (reuses planned ClaimCard /  │ │
│  │   over ONE suggestion engine │  │  EvidencePeek / DiffView)    │ │
│  │ + Y.Doc via Collaboration    │  └──────────────────────────────┘ │
│  │ + cmdk slash / bubble menu   │                                   │
│  └──────────────────────────────┘                                   │
└───────────────┬─────────────────────────────────────────────────────┘
                │ same-origin /api/truth/doc/* (JSON + Yjs update blobs)
                │ + /api/events SSE invalidations
┌───────────────▼─────────────────────────────────────────────────────┐
│ Flask dashboard service (house)                                     │
│  routes -> work_buddy.truth engine (house, K0/K1)                   │
│  server-side gesture minting, stale-view hash checks                │
└───────────────┬─────────────────────────────────────────────────────┘
                │ library calls
┌───────────────▼─────────────────────────────────────────────────────┐
│ <scope-root>/.wb-truth/  (one sidecar, one boundary)                │
│  store.db: claims, evidence, gestures + NEW documents,              │
│    document_spans, expressions, edit proposals, doc events          │
│    (additive DDL, profile-gated)                                    │
│  runtime/: Y.Doc update log + snapshots (CANONICAL document         │
│    object for registered docs, gitignored binary)                   │
│  the Markdown FILE stays in its natural repo location as the        │
│  materialized projection (clean, native, drift-managed)             │
└─────────────────────────────────────────────────────────────────────┘
Agents: MCP capabilities (truth_doc_* propose surface) via the gateway.
Collab phase adds: Hocuspocus (Node sidecar service) + y-tiptap between
editor and store, with onStoreDocument/webhook persistence hooks.
```

## 2. Canonical source of truth (the one decision everything hangs on)

Kaden's invariant [distilled §1]: the canonical document is **richer than Markdown**, must **materialize to Markdown**, and must **feel Markdown-like to edit**. Markdown-canonical was explicitly rejected in the Old-PRD conversation, and y-prosemirror documents that JSON serialization loses collaboration history, so the binary Y.Doc is primary storage [distilled D3, gotchas]. This aligns with the kernel's class-2 document contract (tms-glue §II.4: instrumented surfaces get character-level truth, files get file-level honesty).

| State | Canonical home | Everything else is |
|---|---|---|
| Review state (proposals, decisions, gestures, claim lifecycle) | the scope's `.wb-truth/store.db` ledger | projection. Suggestion marks RENDER proposals, they never carry them. A dropped mark re-anchors from the ledger by quote (kernel `anchors.py`, the AOV firewall) |
| Registered documents (class 2, surface-owned) | the Y.Doc update log + snapshots under `.wb-truth/runtime/`, plus ledger rows for review/provenance state | the `.md` file is the materialized projection in its natural location: clean native Markdown, no markup walls, front-matter stamp only (kernel projection contract). Out-of-band file edits are legitimate INPUT, detected by hash and reconciled back as proposals, never silently absorbed and never overwritten |
| Unregistered files (class 3) | the file itself | untouched by this surface. Capturable as evidence like any file |
| Authorship and interaction provenance | the append-only ledger (document events + kernel producer-identity laws) | in-document marks/attrs are working metadata, the ledger is the truth [distilled D7] |

Two consequences worth stating plainly:

1. **No dual truth.** The file is never a second canonical: it is regenerated from the document object, and human edits made directly to the file re-enter through the drift/reconcile contract (minimal hash-guard + diff-review in v1, full claim-mapped reconcile when the K3 machinery lands).
2. **v1 persists the Y.Doc from day one.** An earlier draft of this document proposed an ephemeral per-session Y.Doc with the file canonical at rest. That contradicted Kaden's richer-than-Markdown invariant and is withdrawn. No WebSocket server is needed for this: the single client loads snapshot + updates over HTTP, posts update batches back, and SSE invalidates other views. Hocuspocus enters only when live multi-client collab does.

## 3. Component table

### 3.1 Editor substrate (client)

| Component | Role | Mode | License | Fit and verdict |
|---|---|---|---|---|
| `@tiptap/core`, `@tiptap/starter-kit`, `@tiptap/react`, `@tiptap/pm` | headless ProseMirror editor, React binding | 1 | MIT | Canonical UI substrate, locked by Kaden [distilled D2]. Chosen over Lexical for ProseMirror's single-point `dispatchTransaction` interception (provenance capture), over CodeMirror because canonical is richer than Markdown. React 19 core support is npm-metadata-sourced, NOT doc-backed (docs only warn their UI Components lag on React 18): confirmed by the S0 StrictMode spike [audit A13]. The Markdown typing FEEL comes from core input rules, not the Markdown extension [audit C1]. Free core only, nothing from the paid registry, ever |
| `@tiptap/markdown` | the ONE Markdown parse/serialize projection (official free npm, MarkedJS, CommonMark, GFM flag) | 1 | MIT | Adopt for import and materialization. Docs tag it BETA on every page (the "v3.7" figure is npm-sourced, docs state no version) [audit C2], so exact-pin + the projection-fidelity suite are the guard. It is a JS library: see the serialization-runtime decision in §4 item 3. Known limits designed around: comments dropped, single-child table cells, whole-doc serialization normalizes untouched regions (block-splice is load-bearing) [audit A10, B] |
| Tracked-change engine: `@handlewithcare/prosemirror-suggest-changes` 0.1.8 (winner over `tiptap-track-changes` sungkhum, `prosemirror-suggestion-mode` davefowler, fallback custom on `prosemirror-changeset`) | tracked insert/delete/replace with accept/reject | 5 (vendored at natural home) | MIT | The load-bearing client choice [distilled D9]. suggest-changes' `withSuggestChanges` dispatchTransaction decorator matches "agent edits become suggestions, not mutations". Exactly one engine ever runs, hidden behind a first-party `WbTrackedChangesAdapter` so it stays swappable. VENDORED (Kaden, C1-DECISIONS item 8), not forked: source lives in-tree at `dashboard-react/src/apps/cowork/suggestions/engine/` (Graphiti-prompts house precedent), per-file MIT attribution header + PROVENANCE.md, no `vendor` folder. Local patches add proposal id + epistemic fields + our attribution. Because vendored code compiles against whatever `@tiptap/pm` resolves, the SP-1 `prosemirror-model` identity blocker dissolves (no separate published dependency to dedupe) |
| `@tiptap/extension-collaboration` + `@tiptap/y-tiptap` + `yjs` | CRDT document binding + Yjs undo | 1 | MIT | Adopt from v1 against a local, persisted Y.Doc (no provider). Gotchas baked into the plan: Collaboration replaces the history extension (disable StarterKit UndoRedo), mount only after initial state is applied (UniqueID ordering), and the Yjs-undo-vs-annotation-positions interaction MUST be spiked [distilled gotchas] |
| Tiptap UniqueID extension | stable node identity across split/merge/undo/paste | 1 | MIT, CONFIRMED free public npm [audit D] | Node-level identity only, inline spans get their own anchor namespace [distilled D12]. NOT turnkey: `types` defaults to empty, mount-after-sync ordering is corruption-critical, `filterTransaction` + `isChangeOrigin` required with Collaboration [audit A8] |
| `cmdk` + `@tiptap/suggestion` | slash command / command palette + trigger plumbing | 1 | MIT both | Adopt. Lift Novel's `EditorCommand` wrapper pattern. The official slash-commands page is an unpublished copy-me experiment, confirming first-party wiring is the doc-consistent route [audit C9] |
| `@floating-ui/dom` | bubble/floating menu positioning | 1 (direct, pinned) | MIT | The v3 upgrade guide instructs installing it yourself: a DIRECT dependency in the deviation-1 audit, not transitive [audit C10] |
| `tiptap-apcore` (aiperceivable) | Tiptap commands as schema-validated MCP tools with ACLs and safety tags | spike only | Apache-2.0 | Attractive and dangerous for the same reason: a competing tool registry. Spike behind a `WbEditorToolBridge`, all mutating calls forced through the proposal path, work-buddy's capability system stays canonical [distilled D10]. Default expectation: reject, reimplement the good ideas |

Dropped from an earlier draft: `tiptap-extension-global-drag-handle` (Notion-style block handles). Block-based WYSIWYG editing is rejected for this surface [Kaden, 2026-07-17], the editor reads as a continuous Markdown-like document. Fact update from the audit: an official DragHandle extension is now free OSS in the open Tiptap repo, so the rejection stands on UX grounds, not license [audit C7].

### 3.2 Design-lift only (no code dependency)

| Source | What we lift | Why not depend |
|---|---|---|
| Novel (`surface/novel/`, Apache-2.0) | slash-menu composition over cmdk, bubble-menu + AI-selector UX (its block drag handle is NOT lifted, block UX rejected) | Abandoned (docs literally "TODO: Add features", pinned Tiptap 2.11). Apache-2.0 permits lifting snippets into GPL-3.0 with attribution |
| `anuchin/tiptap-ai-autocomplete` (MIT), `carlrannaberg/text-editor-autocomplete-demo` (no license, pattern only) | ghost-text decoration + Tab-accept + streaming preview patterns | One-commit showcases. Autocomplete is a later phase and first-party small [distilled D11] |
| Tiptap paid products (AI Toolkit, Tracked Changes, Comments, Version History) | API shapes: `addTrackedInsertion/Deletion/Replacement`, `reviewOptions: {mode}`, `acceptSuggestion`, thread lifecycle events | Paid, private registry. Reference designs only, Kaden's standing rule [distilled D5]. Their existence proves the substrate can express our workflows |
| Proof SDK (MIT) | block lineage (hash + revision window), projection health, mutation idempotency/outbox for the agent bridge | Already mined into the kernel (tms-glue §II.2). Runtime rejected |
| AOV (first-party) | proposal-ledger lifecycle, mark dictionary, silence-is-abstain, stale-report gate, adjudicate-routes-to-new-proposal, exactly-one-anchor apply, bodydiff backstop | Port semantics onto kernel tables. AOV stays the Word-based multi-consumer library. `../corpus/aov.md` is the digest |
| Remirror (Annotation / EntityReference extensions, MIT) | annotation-as-decoration patterns, the documented Yjs-undo-breaks-positions warning | Alternative ProseMirror wrapper, not our substrate [distilled §5] |
| CodeMirror 6 + Y.Text (Architecture A of `../research/editing-surfaces.md`) | the Markdown-fidelity bar it set | Superseded: scoped out by the richer-than-Markdown canonical requirement, see §5 |

### 3.3 Sync and persistence (server)

| Component | Role | Mode | License | Fit and verdict |
|---|---|---|---|---|
| Flask dashboard service | same-origin `/api/truth/doc/*`, gesture minting, SSE, Yjs update blob transport | house | GPL-3.0 | The only client-facing write path. Gesture payloads composed server-side (kernel law) |
| `work_buddy.truth` engine | ledger, lifecycle, anchors, gestures | house | GPL-3.0 | Canonical. The surface adds additive tables + ops, never a parallel store |
| Hocuspocus `@hocuspocus/server` + `@hocuspocus/provider` + `extension-sqlite` + `extension-webhook` | Yjs WebSocket backend for live collab | 1, collab phase | MIT | **RATIFIED by Kaden 2026-07-17** (Node-as-runtime-service accepted, "smarter to stick with the native solution"). Verified same day: v4.4.0 released 2026-07-13, 99 releases, Tiptap-team maintained. Maps onto "our own DB schema and webhooks" via `onAuthenticate` / `onLoadDocument` / `onStoreDocument` / webhook extension. Runs as a sidecar-managed service at the collab phase, NOT in v1. (`pycrdt` noted only as historical alternative, no longer under consideration) |
| `redlines` (PyPI) | server-rendered prose diffs (supersessions, snapshot compares, drift review) | 1 | MIT-style | Already committed in the kernel plan |
| `y-indexeddb` | client offline cache | 1, collab phase | MIT | Only with the provider |

### 3.4 Explicitly not used

| Component | Why |
|---|---|
| Tiptap Cloud, AI Toolkit, Comments, Version History, Snapshot Compare, `TiptapCollabProvider` | Paid or private registry. work-buddy stays free, hard invariant. First-party equivalents: comments = flag proposals + rail, versions = snapshots + ledger, AI = work-buddy's own LLM path |
| Lexical | Not disqualified, the recorded fallback if ProseMirror schema work proves too cumbersome [distilled D2]. Weaker per-operation provenance fit |
| `prosemirror-collab` | Central-authority rebasing model, not CRDT. Kaden rejected it directly. Reference only [distilled D6] |
| NextGraph | Real CRDT platform, wrong altitude (data platform, not editor engine), iframe-broker web SDK. Deferred as a possible deeper substrate, never the first foundation [distilled §3] |
| BlockNote | Different layer over ProseMirror, MPL/GPL/commercial split. Its Yjs 14 attribution work remains the WATCH item |
| Milkdown | Markdown-canonical worldview, which this surface rejected |
| Proof SDK runtime, Draftmark, AiEditor, MAGI, liveblocks, Y-Sweet | Rival worldview, licensing (LGPL+commercial, unlicensed), service dependency, or immature. Established verdicts |
| Novel as a dependency | Abandoned. Lift only |
| Forking Tiptap | Last resort only. Build the `@work-buddy/tiptap` extension bundle instead [distilled D4] |

## 4. What we still write first-party (the glue inventory)

Ordered by risk, highest first:

1. **`WbTrackedChangesAdapter` + proposal sync.** The seam between suggestion marks (client) and proposal rows (ledger). Ingest agent proposals into marks, collect per-item human decisions, re-anchor by quote on drift, expire on rewrite. AOV semantics on kernel tables. This is the product.
2. **Document model DDL + engine ops.** `documents`, `document_spans`, `expressions` (the facts-under-a-sentence seam, pulled forward on Kaden's direction, read-mostly in v1), edit proposals with status events, coarse document events (registered, imported, materialized, session), all append-only, profile-gated. Y.Doc snapshot/update persistence under `runtime/`. Exact DDL at contract freeze.
3. **Materializer + drift guard.** Structured doc -> clean Markdown in the file's natural location, front-matter stamp, content hash recorded, dirty-file refusal + diff-review re-import in v1 (full claim-mapped reconcile arrives with the K3 machinery). **Serialization-runtime decision [audit A1]:** the one serializer (`@tiptap/markdown`) is JavaScript, and v1 has no Node runtime, so in v1 BOTH directions (import parse and materialize serialize) run in the dashboard client, the only JS runtime present. The client posts rendered Markdown plus the structured-doc hash, the server verifies and writes through the engine (single writer, atomic, backups). Documented v1 constraint: flows needing (de)serialization require the view open. The sanctioned escalation is a small Node helper on the standalone `MarkdownManager` (docs support headless use), which arrives at latest with the collab-phase Node service. A Python serializer is forbidden (I14) |
4. **`/api/truth/doc/*` routes.** Open/save/propose/decide endpoints, per-item server-composed gesture payloads, stale-view hash rejection, Yjs blob transport, SSE invalidations.
5. **MCP capability surface.** `cowork_doc_list/get`, `cowork_doc_propose_edit`, `cowork_doc_comment`, `cowork_doc_expression_mark`, plus the `cowork_doc_feedback` capture route (Co-work-owned family per the ratified naming scheme, `../../terminology/cowork-scheme.md`, calling into the truth engine). Agents get semantic proposal verbs, never raw CRDT ops, never direct writes to registered documents (the editing-kernel rule). The Chat tab and the document-bound agent ride the HOUSE conversations machinery (`conversation_*` capabilities + the dashboard chat sidebar, the proven "Help me create a job" pattern): zero new chat infrastructure.
6. **Projection-fidelity suite.** Corpus of real repo/vault docs: import -> materialize must be stable and clean, unedited regions byte-preserved (block-splice materialization), unsupported syntax preserved verbatim and flagged, never silently normalized.
7. **Review rail components.** Mark bar (gesture verbs), proposal cards, claim chips, queue list, DiffView reuse. Theme Contract v1 compliant, registered as a `wb.truth` App contribution.
8. **Provenance ledger glue.** Actor/origin tagging through `dispatchTransaction` meta, coalescing rules deferred to the co-think phase but the event shape reserved now [distilled D7, §6].

## 5. The editor-substrate decision, recorded honestly

The May co-think research chose CodeMirror 6 + Y.Text and `../research/editing-surfaces.md` (2026-07-10) reaffirmed it as Architecture A. The Old-PRD conversation supersedes both, and the root cause is a requirement change, not editor taste [distilled §0]:

1. **Kaden fixed the canonical format first: richer than Markdown, materializable to Markdown, Markdown-like to edit.** CodeMirror is the right substrate exactly when Markdown text IS canonical, so it was scoped out, not outvoted. Kaden's 2026-07-17 clarification completes the picture: "Markdown editing" was in contrast to **block-based WYSIWYG** (Notion-style), which is rejected. The surface edits as a continuous document.
2. **The remaining structured-editor choice (Lexical vs Tiptap) fell to ProseMirror's transaction model**: one `dispatchTransaction` interception point for who/what caused exactly this change, which is the natural hook for a per-operation provenance ledger. Lexical stays the named fallback.
3. **The review-first job amplifies the choice**: suggestion UX is ProseMirror's home turf (multiple live MIT engines to fork), and the paid Tiptap products prove the substrate expresses tracked-changes/comments/AI-review, as reference designs.
4. **The ecosystem points here too**: official bidirectional `@tiptap/markdown` (v3.7) for the projection, y-tiptap + Hocuspocus current and MIT, and the Yjs 14 attribution work landing in the ProseMirror family.

What survives from the CM6 direction: Markdown remains the projection everyone reads and diffs, the schema stays constrained to what Markdown expresses (plus our provenance-bearing marks), and a raw-source mode remains possible later without changing the architecture.

## 6. Sequencing consequence (smallest strong design vs most strategic)

- **Smallest strong design (v1)**: Tiptap + official Markdown projection + ONE forked suggestion engine + persisted local Y.Doc + Flask routes + additive store tables. No Node runtime, no WS server, no autocomplete, no comment threads, no presence, no provocations. Fully useful: agents propose, human reviews tracked edits with per-item gestures, accepted edits materialize, everything ledgered.
- **Most strategic addition (later, with co-think)**: Hocuspocus + y-tiptap provider + presence + the interaction-provenance depth (op coalescing, span-level authorship), turning the same surface into the co-think substrate. v1 is built so this is additive: Collaboration is already bound, proposals are already ledger-canonical, the store already owns document identity.
- **Not worth the complexity at any phase**: adopting an editor product for its provenance, embedding provenance in Markdown, a second claim database, paid escape hatches, forking Tiptap.

---

*Provenance: agent-authored 2026-07-17 (review-surface design session, on Kaden's request). Supersedes its own first draft same day (file-canonical-at-rest withdrawn after the Old-PRD distillation surfaced Kaden's richer-than-Markdown invariant), then revised again same day after Kaden's answer round (Hocuspocus ratified, block-based WYSIWYG rejected, drag handle dropped, expressions pulled forward). Component maturity claims verified same day (Hocuspocus v4.4.0, Tiptap Markdown v3.7 official, @tiptap/react 3.28 React 19, Novel abandoned, anuchin showcase). Unreviewed, not canon.*
