# TMS Glue: Kernel Analysis and Committed Implementation Plan

**Status: Part I is the avenue analysis (stable). Part II is the committed implementation plan, authored 2026-07-10, devils-advocate-amended 2026-07-11 (§II.10), and fork-resolved 2026-07-11 with Kaden's answers baked in (§II.9). The consumer-surface design (§II.4b) and the reason-classed rejection design (§II.5) came out of that resolution round. K0 awaits Kaden's red-pen of the whole plan plus [layman-description.md](layman-description.md) sign-off.**

Part I answers: what is the most foundational thing, can wiring be bought, what does the MVP look like. Part II commits: exact components with integration modes, store topology, schema DDL, module and capability surfaces, gate mechanics, phases with tests, risks.

---

# Part I: the analysis

## 1. Naming the foundational component precisely

The kernel is five things, and only five:

| # | Piece | What it is | Where mistakes are expensive |
|---|---|---|---|
| K1 | **Record model** | The tables/fields: evidence, spans, claims, typed links, status events, store header | VERY. This is the schema the ten invariants live in |
| K2 | **Semantics** | The rules the engine enforces: append-only, lifecycle transitions, justification requirements, supersession + sweep obligations, bitemporal columns, anchor validity | VERY. Semantics are the product |
| K3 | **Thin engine** | A Python library: create/migrate store, append operations, lifecycle ops, sweep and as-of queries, validation | Moderately. Replaceable if K1/K2 hold |
| K4 | **Store topology + profiles** | Sidecar-per-scope instances, a profile header that makes a store opinionated, store registry | Moderately. Retrofitting single-store to multi-store later would hurt, so decide now |
| K5 | **Federation contract** | Stable IDs, cross-store reference URIs (store id + record id), search-partition interface | Design now, implement later. Only the ID/URI scheme is unforgivable |

Everything else in the problem map (extraction, verification models, editors, twin views, review dashboards) consumes the kernel. The person-model is a consumer. None of it needs to exist for the kernel to be right, and all of it is wrong if the kernel is wrong.

## 2. The honest wiring inventory

What a working kernel actually requires, and for each piece: build first-party, buy (dependency), lift (port from another project's code/design), or reuse (already in work-buddy).

| Wiring | Verdict | Source and notes |
|---|---|---|
| Embedded storage + WAL + migrations | **Reuse** | SQLite + the `PRAGMA user_version` migrations framework already proven in `work_buddy/entities/migrations.py` |
| Append-only event/ledger discipline | **Reuse pattern** | The events spine (`work_buddy/events/store.py`: dedup, offsets, DLQ) and the entity reference log are the house patterns. The kernel gets its own tables, same discipline |
| Read models / TTL'd projections | **Reuse** | `SqliteRowsStorage` + `PerRecordTtl` (artifact system) |
| Status machine + audited history | **Build (small)** | A `claim_status_events` table + transition validation. No library needed, semantics are ours |
| Typed links + recursive sweeps | **Build (small)** | SQLite recursive CTEs cover "flag all dependents of superseded claim X". No graph engine needed at personal scale |
| Bitemporal columns + as-of queries | **Lift (design)** | Cozo's point-event validity semantics + XTDB's interval projections, hand-rolled (Part II §1) |
| Span anchors | **Build (small) + port** | Web Annotation selector shape (quote + position + content-hash guard). The re-anchoring logic exists in-house: AOV's verbatim-quote firewall. Port, do not reinvent |
| Claim dedup / merge proposals | **Lift (prompts)** | mem0's ADD/UPDATE/DELETE/NOOP decision prompt (`mem0/configs/prompts.py`, Apache-2.0), repurposed as a *proposal classifier* whose output a human confirms. Never auto-apply |
| Contradiction detection hooks | **Lift (prompts + port)** | Graphiti (Apache-2.0): contradiction prompt in `prompts/dedupe_edges.py`, temporal-bounds prompts in `prompts/extract_edges.py`, and the driver-free `resolve_edge_contradictions()` ported. Kernel stores conflict edges, detection is a consumer job |
| Citation/link staleness | **Lift (algorithm)** | Doorstop's fingerprint-gated link: store the target's SHA-256 at review time, the link auto-stales on drift. The strongest single steal in the substrate survey, a few dozen lines |
| Markdown projection / materialization | **Reuse (design)** | The docs-materialization design + knowledge store file patterns. Co-think's sidecar materializer is the same shape |
| Store registry | **Reuse pattern** | The project registry pattern (SQLite registry of known instances + health) |
| Federated search | **Reuse (later)** | Consolidated-index partitions, one per store instance. Contract designed now, wired when two stores exist. DuckDB's SQLite scanner is an optional read-only cross-store rollup tool at the reporting tier |
| Human gates / confirmation flows | **Reuse** | Consent system + conductor workflows + AOV proposal-ledger semantics generalized (L8) |
| Lifecycle notifications | **Reuse** | Events spine (`truth.claim_confirmed`, `truth.claim_superseded`, ...), spine stays non-authoritative |
| PROV export | **Buy (tiny)** | `prov` (MIT) for PROV-JSON export only |
| Local NLI scoring | **Buy (later)** | LettuceDetect + HHEM per the claim-verification report. Not kernel |

Reading of the table: **the wiring is dominated by reuse and small first-party SQL, not by missing infrastructure.** The expensive part is K1/K2 design, not plumbing.

## 3. The avenues, analyzed honestly

### Avenue A: first-party SQLite kernel on house patterns (the baseline)

Design K1-K5, implement K3 thin, reuse everything in §2. **What it costs:** the schema design work (unavoidable in every avenue) plus a small engine library. **What it risks:** schema mistakes, mitigated by append-only + projections and by fixture validation against three workloads before anything depends on it. **What it preserves:** one worldview, house idioms, no impedance with the existing stores, GPL-clean.

### Avenue B: an embedded engine supplies the wiring

The serious candidates, each verified in [research/kernel-substrates.md](research/kernel-substrates.md):

- **CozoDB** (embedded Datalog, MPL-2.0, native time-travel "validity" semantics). On paper the best semantic fit anything offers. Verified: **dormant since December 2023**, stale bindings. Design-study only (study completed, Part II §1).
- **Python `eventsourcing` library** (BSD-3, alive): solves only the write side (the queryable claims table stays hand-built), its aggregate-per-stream DDD framing fights cross-claim operations, and work-buddy already has proven append-only patterns.
- **Oxigraph / pyoxigraph** (MIT/Apache, alive): RocksDB multi-file storage against our one-file-per-scope stores, triple-reassembly joins for what are indexed-column queries in SQL, and a second data idiom. Export-only remains the right PROV posture.
- **DuckDB**: wrong shape on the write path. One verified redemption: its SQLite scanner attaches many `.db` files at once, making it a legitimate *read-only federation* tool over targeted claim stores later. Never the kernel.

Verdict (evidence-backed, §8): engines do not remove the K1/K2 design work, they relocate K3 while adding a worldview. Avenue B is closed except for DuckDB's future reporting role.

### Avenue C: design-lift and sub-piece vendoring (compose with A, not an alternative)

License or "rival stack" status rejects runtimes, not knowledge. Apache/MIT rivals donate code and prompts (Graphiti, mem0, Proof SDK schema patterns). Non-permissive or closed projects (FalkorDB, Kumiho, commercial UX) donate design under study-and-reimplement. The kernel-substrates report inventories the specific files and prompts, and Part II §3 commits each one.

### Avenue D: vocabulary-only start ("could picking vocabularies be enough?")

Direct answer: **no, but it is half the answer.** Vocabulary alignment (PROV terms, PAV roles, Wikidata statement/rank shape, Web Annotation selectors, in-toto envelope) buys field names, mental compatibility, and an export path at near-zero cost. It cannot buy the four wirings that make the kernel real: anchor resolution, the audited status machine, sweep/staleness queries, and cross-store references. Those four are exactly where the lived failures happened. Vocabularies are settled first (they gate the schema), and the MVP's first slice is deliberately schema-heavy and engine-light.

### Avenue E: adopt an existing app as the store

Rejected as runtime (competing worldview and infrastructure on what must remain many small, inspectable, user-owned files), mined for parts via Avenue C.

## 4. What the targeted-store architecture changes

- **Smaller, sooner stores.** A store instance for one purpose can ship with a partial profile and real utility. No universal ontology needed on day one.
- **Profiles become kernel-level** (K4): "opinionated store" is the product shape ("designed for co-think editing" vs "designed for source-to-paper management"), the way `.aov.yaml` configures AOV per consumer.
- **The ID/URI scheme is promoted to unforgivable**: cross-store references cannot be retrofitted onto ambient integer ids.
- **Federation is a contract, not a component.** Nothing built until two stores exist, but the schema carries what federation needs.
- **Privacy containment comes free.** Sensitive facts live in their own store file (the confine-sensitive-topics rule made physical). Whole-store deletion is always possible by deleting the directory.

## 5. The MVP definition

Kernel v0 is a design artifact set plus the thinnest engine slice that can hold real records: (1) the record model, (2) validation against three contrasting workloads (an electricrag canon file, a my-career fact set, a co-think session), (3) store topology + ID/URI + profile decisions, (4) a thin engine slice exposed as capabilities so canonize-style manual workflows write real records immediately. Explicitly out: extraction pipelines, verification models, the editor, twin views, federated search implementation, any automation of confirmation. Part II turns this into phases with tests.

## 6. Decision table

| Avenue | Removes design work? | Removes wiring? | New worldview cost | License risk | Verdict |
|---|---|---|---|---|---|
| A: first-party SQLite kernel | No (nothing does) | Via §2 reuse, most of it | None | None | **Committed** |
| B: embedded engine | No | Some query wiring | Datalog/RDF dialect, second idiom | MPL fine, others fine | **Closed by verification** (Cozo dormant, eventsourcing half-fit, oxigraph wrong shape). DuckDB survives as optional read-only federation tool |
| C: design-lift + sub-piece vendoring | Yes (solved sub-problems) | Yes (prompts, schemas, algorithms) | None (ported into our idiom) | Study-only for non-permissive | **Committed as method**, per-component commitments in Part II §3 |
| D: vocabulary-only | Half (settles names/fields) | No | None | None | **Committed as step one of A**, insufficient alone |
| E: adopt an app | No | Superficially | Severe (rival worldview) | Varies | Rejected as runtime, mined via C |

## 7. Former open questions (now resolved or escalated)

1. **Sidecar naming and granularity:** committed in Part II §4 (`.wb-truth/` per scope root). The co-think unification question is escalated to Kaden (Part II §9, Q1).
2. **First validation workload pairing:** committed: my-career person-facts + electricrag project-canon, with a co-think session as the third *fixture-only* workload.
3. ~~Datalog appetite~~ resolved by evidence: Cozo dormant, Avenue B closed (§8).
4. **Profile authoring surface:** committed: `store.yaml` in the sidecar (AOV-style config) + one knowledge-store directions unit per profile for agent behavior. Both, each doing the job it is shaped for.
5. **Kernel name:** committed provisionally as `truth` (`work_buddy/truth/`, `.wb-truth/`, `truth_*` capabilities). Ratification escalated (Part II §9, Q3) since product-facing naming is Kaden's call.

## 8. Substrate verification results (verified 2026-07-10)

Full report with sources: [research/kernel-substrates.md](research/kernel-substrates.md). Fourteen candidates checked live. The synthesis:

**Avenue B is closed by evidence, not preference.**
- **CozoDB is dormant** (last release December 2023, stale Python bindings). Its native `Validity` time-travel keys remain the closest thing to bitemporal-by-construction anywhere, worth studying as a schema idea, dead as a dependency.
- **`eventsourcing`** (BSD-3, alive) solves only the write-side half while the queryable claims table stays hand-built anyway, and its aggregate-per-stream DDD model fights a ledger whose interesting operations (supersession, conflict) inherently cut across two claims. Optional prototype at most.
- **Oxigraph** (MIT/Apache, alive, mature) is the wrong data shape and the wrong file model: RocksDB multi-file directories against our one-file-per-scope stores, and triple-store ergonomics against a status-machine workload that is SQL's home turf. PROV export stays with the tiny `prov` package.
- **DuckDB** is wrong on the write path but earns a narrow future role: its SQLite scanner can attach many per-project claim stores for read-only cross-store rollups. Reporting tier only, never the write path.
- **Kuzu forks** (Vela-Engineering, LadybugDB, bighorn) are months old. Watch, re-check in six to twelve months.
- **No living library** does two-axis bitemporal SQLite, and **no usable TMS/ATMS library exists** (only student exercises, now confirmed rather than assumed). The bitemporal pattern and the justification machinery are first-party by elimination.

**The buyable wiring is prompt-sized and algorithm-sized, and it is genuinely good:**
- **Doorstop's fingerprint-gated link** is the standout steal of the survey: a link stores the target's SHA-256 at review time, so the link goes stale automatically the instant the target drifts. Citation-staleness solved in a few dozen first-party lines. LGPLv3 would even permit linking, but the item-per-YAML storage is the wrong shape, so port the algorithm only.
- **Graphiti's prompts are copy-ready** (Apache-2.0, license held through Zep's 2026 pivot): the contradiction-detection prompt (`graphiti_core/prompts/dedupe_edges.py`), the temporal-bounds prompts (`extract_edges.py`), and the driver-free `resolve_edge_contradictions()` (port, small). Never import `graphiti_core` (it would drag a second identity system in).
- **mem0's ADD/UPDATE/DELETE/NOOP prompt** (`mem0/configs/prompts.py`, Apache-2.0) is already decoupled at the LLM-call boundary. Repointed from "mutate now" to "open a proposed status transition for human confirmation", it becomes our dedup/supersession proposal classifier.
- **simonw's sqlite-history** trigger-and-bitmask pattern is a clean transaction-time building block (design-lift only, and probably unnecessary given append-only base tables).
- **nanopub-py** (Apache-2.0, active) is the most literal "one claim plus its provenance as a citable unit" implementation found. A plausible future signed-export sidecar next to `prov`, nothing now.

**Confirmations rather than imports:** Cognee's pipeline shape and two-field provenance stamp, basic-memory's Markdown-source-plus-SQLite-index split (AGPL-3.0, a live section-13 obligation for a dashboard-serving tool, so design-only), and memobase's YAML profile taxonomy all independently converge on patterns work-buddy already has. The Proof SDK schema gap flagged here has since been closed by a direct source read (Part II §2).

**Net:** the yield of a fourteen-candidate hunt for buyable wiring is a handful of prompts, one hashing algorithm, and one trigger pattern, each faster to reimplement than to carry as a dependency. The first-party SQLite kernel stands on evidence.

---

# Part II: the committed implementation plan

Everything below is committed unless it appears in §9 (forks reserved for Kaden). Committed means: this is what gets built, in this order, with these components at these integration modes, subject only to Kaden's red-pen and the devils-advocate findings appended in §10.

## II.1 Cozo Validity study: outcome

Studied from the primary docs (docs.cozodb.org/en/latest/timetravel.html), mechanism confirmed: a fact is a **point event** `(key..., Validity(timestamp, assert_flag))`, histories are rows sharing a key prefix, and "the fact represented by a row is valid from its timestamp up until the timestamp of the next row under the same key." Retraction is a `false`-flag row. As-of queries resolve against the snapshot at `@ T`. Currency queries take the first row under descending sort. Same-instant collisions resolve assert-over-retract.

**What it changes (three commitments):**

1. **Intervals are derived, never hand-maintained.** `valid_to` on a claim is optional and usually absent. When a confirmed claim is superseded by a successor with reason `updated`, the predecessor's effective valid interval *ends where the successor's `valid_from` begins*, computed in the projection, exactly Cozo's next-row-under-same-key rule transplanted onto supersession chains. Nobody has to remember to close an interval, which is the ergonomic failure mode of four-column bitemporal by hand.
2. **Explicit `valid_to` is reserved for known closures** ("employed at X until 2022", "this preference lapsed"), written at propose/confirm time on the immutable row, and a valid-time closure of an existing fact is itself a supersession (reason `valid_time_closed`), never an UPDATE.
3. **Index for currency reads follows Cozo's descending trick:** `(scope, subject_key, valid_from DESC)` so current-fact lookup is first-row-wins.

**Vendor verdict, final:** CozoDB is not vendored, not depended on, not forked. Integration mode: **design-lift only**, realized as the three commitments above. Nothing else in the plan changed.

## II.2 Proof SDK schema gap: closed

Direct source read of `EveryInc/proof-sdk` `server/db.ts` (MIT). Finding: **Proof's relational schema is an editor-runtime store, not a claim ledger.** Documents carry `marks TEXT DEFAULT '{}'` (provenance lives inside a JSON blob, not relational rows), plus an event log, Yjs update/snapshot tables, and mutation idempotency/outbox tables. There is no claims/review relational schema to mirror. Three patterns are worth lifting as design:

1. **Block-level lineage**: `document_blocks(block_id, markdown_hash, created_revision, last_seen_revision, retired_revision)` is a clean block-granular anchor pattern (hash + revision lifetime window). Lifted into co-think's phase, not the kernel.
2. **Projection health**: `document_projections(health, health_reason)` makes projection freshness first-class. Lifted into our `claims_current` projection metadata.
3. **Mutation idempotency + outbox**: relevant to co-think's agent bridge later, noted, not kernel.

The provenance FIELDS worth mirroring come from Proof's provenance spec, already digested in [corpus/co-think-research-digest.md](corpus/co-think-research-digest.md). Honest mapping of where each lands (corrected after the second devils-advocate round): `origin human|ai` → `created_by_kind`/`author_kind` (K0). `model` and the authoring context → the mandatory producer-identity meta keys on agent writes (K0 invariant). `basis described|inferred|suggested` → a documented, engine-enforced `meta_json` key on agent-proposed claims (K0 convention, real column in a v2 migration if queries demand it), and per-SPAN basis lands with co-think's own span table (additive, that phase). `typed_by/inserted_by` and review-staleness-per-span → co-think's span table, additive. Integration mode for Proof SDK, final: **design-lift only**, spike C (embedding its editor) remains a co-think-phase option, irrelevant to the kernel.

## II.3 Committed component table

Integration modes: **1** upstream dependency, **2** VCS-pinned, **3** tracking fork, **4** hard fork, **5** vendored source, **port** = first-party reimplementation from their design, **lift** = design/pattern only, **house** = existing work-buddy machinery.

### Adopted

| Component | Mode | Phase | License | Role |
|---|---|---|---|---|
| SQLite + WAL + `user_version` migrations | house | K0 | n/a | store engine, following `entities/migrations.py` |
| uuid4 hex ids (house convention) | house | K0 | n/a | record ids (considered ULID, rejected: zero-dep consistency wins, `created_at` covers ordering) |
| AOV quote re-anchor firewall | port (from own repo) | K0 | first-party | anchor resolution + hallucination-proof quoting |
| Doorstop fingerprint-gated links | port | K0 (column) + K3 (sweep) | LGPL-3.0 (algorithm ported, no dep) | link staleness |
| Cozo Validity semantics | lift | K0 | MPL-2.0 (concepts) | derived intervals, §II.1 |
| XTDB bitemporal projections | lift | K0 | concepts | as-of query shape |
| Web Annotation selectors | lift | K0 | W3C spec | anchor JSON shape |
| Wikidata rank + typed deprecation reasons | lift | K0 | concepts | supersession-reason enum |
| PAV author/contributor/curator roles | lift | K0 | concepts (ontology dormant, harmless) | actor-role enum |
| Proof provenance-spec fields | lift | K0 | MIT (concepts) | span provenance + review staleness semantics |
| events spine notifications | house | K1 | n/a | `truth.*` lifecycle events, non-authoritative |
| consent + `user_initiated()` + conductor | house | K1 | n/a | gate wiring |
| project-registry pattern | house | K1 | n/a | store registry |
| mem0 update-decision prompt | port (rewritten propose-only, attributed) | K3 | Apache-2.0 | dedup/supersession proposal classifier |
| redlines | 1 (PyPI) | K3 | MIT-style | server-side reviewable diffs |
| Graphiti prompts (`dedupe_edges.py`, `extract_edges.py`) | 5 (vendored prompt text, attribution header) | K4 | Apache-2.0 | contradiction + temporal-bounds detection |
| Graphiti `resolve_edge_contradictions()` | port | K4 | Apache-2.0 | deterministic invalidation proposals |
| Claimify three-stage recipe | port (published method, no official code) | K4 | paper | claim decomposition |
| lettucedetect | 1 (PyPI) | K4 | MIT | local entailment scoring |
| HHEM-2.1-Open | 1 (HF weights, pinned revision) | K4 | Apache-2.0 | cheap entailment pre-filter |
| prov | 1 (PyPI) | K5 | MIT | PROV-JSON export only |
| swh.model | 1 (PyPI), OPTIONAL | K2 | GPL-3.0 (license-identical) | validated SWHID builder for git-sourced evidence (core hash also computable first-party, §II.4 registry) |
| manubot (cite machinery) | 1 (PyPI) | K4 | BSD-3 | DOI/PMID/arXiv resolution to CSL-JSON for academic evidence |
| consolidated-index partitions | house | K5 | n/a | federated search: one partition per registered store, embedded and refreshed by the existing embedding service (per-partition freshness crons + consumer opt-in flags, the proven index-consolidation pattern) |
| inference-provenance design | related, not required | when built | n/a | optional join enriching agent-run refs with per-call records. The ledger does NOT depend on it: producer identity is self-carried inline (see §II.5 invariants) because that design's JSONL ages out on a 14-day TTL |
| DuckDB | 1 (optional) | K5 | MIT | read-only cross-store rollups, never write path |
| in-toto statement envelope | lift | K5 | concepts | derivation export shape |

### Considered and not adopted (final)

| Component | Reason |
|---|---|
| CozoDB runtime, Kuzu forks, eventsourcing, oxigraph | Avenue B closed (§8). Kuzu forks re-checked in 6-12 months |
| Cognee, mem0/memobase runtimes, basic-memory code, Wikibase, TerminusDB, Dolt, XTDB runtimes | rival worldview or wrong substrate, design already extracted |
| python-ulid | uuid4 house pattern suffices |
| sqlite-history | append-only base tables make column-bitmask history redundant |
| nanopub-py | deferred, mode 1 only if a signed-export feature is ever built |
| BlockNote / Yjs 14 attribution | co-think phase concern, watch item, unreleased |
| DeepEval, RefChecker, Bespoke-MiniCheck-7B | telemetry exfiltration / archived / CC BY-NC, per claim-verification report |

## II.4 Store topology, ids, profiles, backup (committed)

**Sidecar layout.** One store per scope root:

```
<scope-root>/.wb-truth/
  store.db          # the kernel store (SQLite, WAL)
  store.yaml        # profile + store identity echo (human-readable)
  blobs/<sha256>    # large evidence snapshots (content-addressed)
  exports/          # PROV-JSON and other derived exports (rebuildable)
```

`<scope-root>` is typically a repo root (`electricrag/.wb-truth/`) or a purpose directory (`my-career/.wb-truth/`). Multiple stores per machine are the normal case. work-buddy's own preference canon lives at `<data_root>/truth/personal/.wb-truth/`.

**Git posture and portability.** `store.db` and `blobs/` are gitignored by a generated `.wb-truth/.gitignore` (binary churn does not belong in consumer repos). `store.yaml` is committed. Two durability paths, both committed here:
- **Machine durability:** the store registry feeds registered store paths into work-buddy backup coverage (K1 acceptance includes this wiring).
- **Repo-travel durability (the canonical text form):** the engine maintains `.wb-truth/export/claims.jsonl`, a deterministic, append-ordered JSONL export of the full ledger (records in insertion order, stable field order), regenerated on write. **Committed to the consumer repo by default** (Kaden, Q6 resolution: these repos are private, versioning is wanted). A profile may opt out (`export_committed: false`) for future public or shared repos. It diffs cleanly in PRs, travels with clones, and `truth_store_import` rebuilds a working `store.db` from it on any machine, preserving `store_id`. The registry refuses two live stores with the same `store_id` at different paths (restore-beside-live is an explicit error, resolved by the human choosing which is live). Real multi-machine concurrent WRITE sync is explicitly out of scope for v0 (documented limitation: one writing machine per store at a time, git is the transport).

**Identity.** `store_id` = uuid4 hex minted at creation, echoed in both `store.yaml` and `store_info`. Record ids = uuid4 hex. Cross-store reference URI: `wb-truth://<store_id>/<kind>/<record_id>` where kind is `claim`, `evidence`, `span`, `derivation`. Entity references use the existing registry's ids as soft URIs (`wb-entity://<id>`), no cross-database foreign keys, resolution stays the registry's job.

**Registry.** `truth_stores` registry (path, store_id, profile, title, last_seen, reachable), following the project-registry pattern, living in work-buddy's data root. Stores are discovered by registration (at create) and re-validated on access.

**Projection contract (normal Markdown, drift-managed). Committed 2026-07-11 after Kaden's drift question.**
- **Projections are NATIVE Markdown.** No span ids, no per-block identifiers, no YAML-comment walls in the body (Proof's embedded-YAML wall was flagged in the co-think research as exactly the anti-pattern). The only in-file marker is minimal front matter: the source `store_id` and a generated-file notice. Everything else the system needs lives in the SIDECAR, not the file: at render time, `materialize.py` writes a **render manifest** into the store (per-claim rendered regions: claim id, heading path, quote, offsets, plus the whole-file content hash). The file stays clean because the bookkeeping has somewhere else to live.
- **Residency is per-profile** (`projection: resident | on_demand | none`). Canon profiles default to resident, because those files are read by humans, other agents, and git PRs when no engine is running. Residency is what makes drift management mandatory rather than optional.
- **Drift is detected mechanically.** The store knows the exact hash of what it last rendered. A freshness sweep (and every re-materialize attempt) compares: file hash differs from last-rendered hash means `drifted`, recorded on the projection row (Proof-style health fields).
- **Human edits are never overwritten and never ignored: they are RECONCILED.** materialize refuses to overwrite a dirty projection. The reconcile flow diffs the edited file against the last-rendered content, maps changed hunks to the claims that rendered them via the manifest (quote-based re-anchoring when offsets shifted, the AOV firewall), and opens **supersession proposals** for the affected claims (and new-claim proposals for inserted assertions) in the review queue. Attribution is honest: a raw file edit is `unattested` (any editor, human or agent, could have made it), so the human's one-click confirm in the queue is what supplies the attestation, exactly the same gate as everything else. Deletions in the file propose retractions. Non-assertive edits (typo fixes, formatting) reconcile as re-render without any claim change.
- **The file is therefore a legitimate INPUT surface, not truth.** Editing canon Markdown in Obsidian keeps working, and what you did becomes reviewable truth changes mapped to the right claims instead of silent divergence. The two failure modes this kills: the store never learning about a fact-changing edit (drift), and the system stomping human words (overwrite).
- **The machine-readable twin already exists.** `export/claims.jsonl` is the committed, versioned, lossless file form. The Markdown projection is purely the human-comfort layer, which is why it can afford to stay native and pretty.
- Span-level authorship of live edits is explicitly NOT this contract's job: that requires an instrumented editing surface (co-think's CRDT op log). Plain files get file-level honesty (`unattested` + reconcile-through-review), instrumented surfaces get character-level truth. Both feed the same ledger.

**Two span relations and three document classes (committed 2026-07-11, Kaden's expression question).**

The word "span" hides two OPPOSITE relations, and the design treats them as distinct record kinds because they differ in mutability, lifecycle, and verification direction:

| | **Support** (epistemic, built K0) | **Expression** (compositional, deferred by design) |
|---|---|---|
| Reads as | "this evidence span is how we KNOW the claim" | "this document passage is where we SAY the claim" |
| Direction | evidence span → claim (upstream) | document span → claim (downstream) |
| Span side | immutable (evidence is a snapshot) | mutable (documents get edited) |
| Claim side | the thing being justified | the thing being leveraged, quoted, or paraphrased |
| Staleness source | source retraction (rare) | either side: claim superseded, or passage edited away |
| Verification arrow | span must ENTAIL claim (NLI: premise = span) | claim must ENTAIL passage (NLI: premise = claim). Same machinery, swapped roles |
| Table | `supports_span` links onto `evidence_spans` (K0) | render manifests now, first-class `documents`/`document_spans`/`expressions` tables in co-think's phase (reserved names, additive DDL, confirmed safe to defer in §II.10b) |

**Three document classes** decide how much expression machinery a document needs, and this is why nothing heavy gets built up front:

1. **Generated projections (class 1).** Documents 100% derivable from the store: canon files, resumes, bios. Their span-to-claim map IS the render manifest, rebuilt on every render, no independent truth to maintain. This covers everything through K4: the resume artifact is itself a generated projection, so its line-to-claim citations live in its manifest, and the K4 entailment gate verifies manifest entries (claim must entail line). Canon files qualify as class 1 precisely because the canonize discipline already makes them claim-shaped: verbatim-approved declarative prose means the claims' propositions ARE the file's sentences, and rendering ordered propositions reproduces the file faithfully.
2. **Co-authored living documents (class 2).** Papers, essays, design docs: human-owned prose that *uses* claims but is not derivable from them. These need real `expressions` rows (span anchors on a mutable document, role: quote | paraphrase | summary | instantiation, claim-side fingerprint = `canonical_sha256` so claim supersession mechanically stales the expression, span-side fingerprint for edit drift). This is co-think's phase and the level-2 document-anchored view's substrate. NOT built in K0-K5. The upgrade path is one additive migration: when a class-1 document graduates to co-editing, its manifest entries convert into expression rows and the CRDT surface maintains them from then on.
3. **Plain files (class 3).** Never registered, nothing tracked. Capturable as evidence like any other file. Most files in any repo stay class 3 forever.

**Same store, different tables, gated by profile.** Expression records live in the SAME per-scope store as the claims of that scope, never a separate database: the supersession sweep must traverse claim-to-expression in one query, and a scope's claims and its documents share one lifecycle, backup, and privacy boundary. "Document store" is therefore a PROFILE (`cothink-doc` activates the document tables), not a different kind of store. Cross-scope expression (a manuscript in electricrag expressing a personal-store preference) uses `wb-truth://` claim URIs exactly like cross-store derivation premises, resolved through the registry at sweep time, fail-soft (unresolvable = flagged, never silently dropped).

**Profiles.** `store.yaml` declares: `profile` name (e.g. `project-canon`, `person-facts`, `cothink-doc`), allowed `claim_kinds`, per-kind required fields, gate policy (`rejected_content: redact | retain`, confirmation surfaces allowed, and `block_materialize_on_flags: bool`, which refuses to regenerate or ship a projection while unresolved needs_review or verification flags exist in its scope: the ledger-level analog of a blocking lint, extracted from Kaden's pre-truth-layer notes, corpus/kaden-notes-2026-07-11.md), and validator hooks. The engine validates propose/confirm operations against the profile. Each profile additionally gets one knowledge-store directions unit (agent behavior guidance), authored at K2 when the first two profiles ship. Profiles are data, adding one never changes engine code.

**Source-locator scheme registry (committed 2026-07-11, extracted from Kaden's pre-truth-layer notes via research/source-locators.md).** `evidence.source_locator` stops being format-free text: it is a URI whose scheme comes from this registry, with a `locator_scheme` discriminator key documented in `meta_json`. A spec on top of the committed schema, no migration (`content_sha256`, spans, and `acquired_at` already carry the load). Per kind:

| Evidence kind | Canonical locator | Verifiability class | Integrity recipe |
|---|---|---|---|
| Git-sourced content | **SWHID** (ISO/IEC 18670:2025), fully qualified (core `swh:1:cnt:<hash>` + origin/anchor/path/lines qualifiers). The core hash IS the git blob hash, mintable locally with zero dependencies and no archive. A GitHub/GitLab permalink is stored in meta as the display form | A: content-addressed, eternally re-derivable | recompute the blob hash at the anchored commit, verify the span quote at the cited lines |
| Web page | live URL + `retrieved_at` + archived URI (Memento RFC 7089 / Wayback) or local snapshot blob. W3C Web Annotation States (TimeState, HttpRequestState) ride inside the existing `selector_json` | B when snapshotted, D (rot-prone) as bare URL | hash-match the snapshot, flag bare-URL evidence older than a profile-set age |
| Academic publication | DOI (or arXiv id / PMID) in the locator, **CSL-JSON** in `meta_json`, page/section pinpoint as the span | C: resolver-dependent, promoted to B with a stored snapshot hash | resolve the identifier, match CSL metadata, verify snapshot hash when present |
| Chat / utterance | `wb-session://<session_id>/<message_ref>` (internal scheme) | B: transcript blob hashed at capture (transcripts TTL out, the blob is ours) | blob hash check |
| Local file / artifact | path + `content_sha256` (already committed) | A when blob-snapshotted | existing hash check |

Unified-standard verdict (research/source-locators.md): none exists. RFC 8574 `cite-as` and FAIR Signposting are discovery conventions, not locator formats, so a small per-kind registry with URIs everywhere is the honest end state. Tooling adopted: `swh.model` (GPLv3, license-identical to work-buddy, mode 1, OPTIONAL: the core hash is computable without it) at K2 when project-canon capture lands, `manubot`'s cite machinery (BSD-3, mode 1, resolves DOI/PMID/arXiv to CSL-JSON) at K4 with the citation-integrity consumer. Integrity recipes attach to the existing K3 sweep. Capture helpers validate locators against the registry per kind from K1 onward.

## II.4b Consumer surface: how other repos use this (Q3 resolution, first principles)

Kaden's Q3 exposed a real gap: an earlier convergence.md claim that "the MCP gateway is machine-global, so sessions in other repos consume it through wb_run" was **wrong as a consumer story**. The wb MCP server is configured for work-buddy sessions, and even if other repos could mount it, they should not: the full gateway surface plus work-buddy's context is exactly the bloat a focused electricrag or my-career session does not need. Corrected here from first principles.

**Separate the three planes:**

1. **Data plane: where truth lives.** Already settled and unchanged: per-scope stores inside consumer repos (`electricrag/.wb-truth/`), with the committed JSONL export as the diffable repo-travel form. One addition that answers "files stored where the file should live": **human-facing canon files remain real files in their natural locations.** `electricrag/docs/canon/*.md` continues to exist, maintained as a **materialized projection** of the store (the docs-materialization pattern, `materialize.py`). Humans read and diff files where they always did. The store is the ledger behind them, not a replacement for having files.

2. **Invocation plane: how agents and humans in a consumer repo call the engine.** **CLI-first: `wbuddy truth <verb>`.** The `wbuddy` entry point already exists machine-wide (`~/.local/bin` shims from the uv migration) and the engine is a library, so the CLI opens the store directly with no gateway, no MCP mount, and no server dependency for reads and proposes. Engine-level gates are entry-point-independent (the actor rules live in the engine, not the transport), so this adds no bypass: agents invoking the CLI can capture evidence, propose, and query, and CANNOT confirm, because confirm requires a gesture minted by an interactive surface. CLI confirm exists only as (a) an interactive TTY review session run by the human, or (b) `--gesture <id>` referencing a gesture already minted by the dashboard. The TTY check is a local-trust convention, consistent with the raw-file honesty paragraph in §II.6: the owning human is not the adversary, agents are gated structurally by the engine.

3. **Context plane: what a consumer repo's sessions need to know.** One short rules file, not the work-buddy world. **`wbuddy project init`** (new, K2) scaffolds a consumer repo: creates `.wb-truth/` with the chosen profile, registers the store (registry + backup coverage), writes the generated `.gitignore`, and drops a ~20-line agent-facing snippet (`.claude/rules/wb-truth.md` or a CLAUDE.md section, consumer's choice) documenting the store's existence, the profile's citation discipline, and the five CLI verbs. That snippet is the ENTIRE work-buddy context a consumer session carries.

**Why not the alternatives:**
- **Full wb MCP in consumer repos:** context and tool-surface bloat, rejected by Kaden directly.
- **A scoped/minimal truth MCP endpoint** (the mode-registry "effective capability view" applied cross-repo): genuinely attractive later for tool-call ergonomics, and cheap once ops exist (same registry, filtered surface). Deferred as a K5-adjacent option, not v0: the CLI covers the need without new server surface.
- **Vendoring the engine into consumer repos (AOV/AEXP pattern):** wrong fit for the kernel. AOV is a multi-user library consumed by OTHER PEOPLE'S repos, so vendoring pins versions per consumer. Truth stores are machine-local and engine-versioned together with work-buddy: vendoring would create N engine copies with schema-version skew against live stores. The AOV-shaped thing here is the PROFILE (config-only, per-repo), not the engine.

**The messaging precedent, acknowledged:** the inter-project messaging system (hooks, `/tmp/wb/send`) already is a proto consumer surface, and it is hacky by Kaden's own assessment. `wbuddy project init` is deliberately shaped to absorb it later (init becomes the one command that makes any repo a work-buddy-aware consumer: truth store, messaging hooks, session context). Messaging unification is OUT of this plan's scope, recorded as the natural follow-up.

**Phase impact:** K1 gains the `wbuddy truth` CLI verbs (thin wrappers over the same engine ops used by the gateway). K2 gains `wbuddy project init` and uses it to onboard electricrag and my-career (dogfooding the consumer story from day one). The K2 exit criteria gain: a fresh session in the consumer repo, carrying only the init-generated snippet, successfully proposes a sourced claim via CLI.

## II.5 Schema v0 (committed DDL)

Design rules embodied: append-only base tables enforced by triggers, immutable claim rows (change = new row + supersedes link), derived intervals (§II.1), three clocks (valid time on claims, transaction time on rows/events, decision time on gestures), reified derivations, Doorstop fingerprints on links, controlled redaction as the only sanctioned mutation.

```sql
-- store identity (single row)
CREATE TABLE store_info (
  store_id TEXT PRIMARY KEY, profile TEXT NOT NULL,
  schema_version INTEGER NOT NULL, title TEXT, created_at TEXT NOT NULL);

-- L1: immutable observations
CREATE TABLE evidence (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,             -- document|web|chat|utterance|artifact|import
  source_locator TEXT NOT NULL,                        -- path / URL / session id / commit, per kind
  content_sha256 TEXT NOT NULL, content TEXT,          -- inline snapshot when small
  content_path TEXT,                                   -- blobs/<sha256> when large
  media_type TEXT, acquired_at TEXT NOT NULL,
  acquired_by_kind TEXT NOT NULL, acquired_by_ref TEXT, -- human|agent_run|system + ref (session/call id)
  acquisition_method TEXT NOT NULL,                    -- fetch|paste|import|said_in_chat|file_read
  trust_class TEXT NOT NULL,                           -- user_authored|user_curated|agent_authored|mixed|unattested|external|external_quarantined
                                                       -- unattested = DEFAULT for pre-existing files (problem-map L3 rule).
                                                       -- agent_authored/mixed exist so "an AI wrote this" is never
                                                       -- recorded as "unknown" when the capturer knows better
  derived_from_store TEXT,                             -- store_id when this evidence is a captured PROJECTION of a
                                                       -- truth store (materialize.py stamps outputs): anti-cascade flag
  meta_json TEXT, redacted_at TEXT, created_at TEXT NOT NULL);

CREATE TABLE evidence_spans (
  id TEXT PRIMARY KEY, evidence_id TEXT NOT NULL REFERENCES evidence(id),
  selector_json TEXT NOT NULL,                         -- Web Annotation: exact/prefix/suffix + start/end
  quote_exact TEXT, span_sha256 TEXT NOT NULL,
  author_kind TEXT, author_ref TEXT,                   -- who wrote THIS SPAN when finer than the evidence row's
                                                       -- trust_class (human|agent_run|unknown): mixed-authorship
                                                       -- transcripts resolve authorship at the span, where support attaches
  redacted_at TEXT, created_at TEXT NOT NULL,
  created_by_kind TEXT NOT NULL, created_by_ref TEXT);

-- L2: immutable assertions
CREATE TABLE claims (
  id TEXT PRIMARY KEY, proposition TEXT NOT NULL,      -- canonical prose form
  canonical_sha256 TEXT NOT NULL,                      -- sha256 over canonical JSON (sorted keys) of
                                                       -- {proposition, claim_kind, structured_json, scope, valid_from, valid_to}
                                                       -- computed by the engine at insert, survives redaction,
                                                       -- THE hash gestures bind to and dedup keys on
  claim_kind TEXT NOT NULL,                            -- profile-constrained OPEN set (profiles may mint kinds), core: fact|preference|definition|measurement|decision_outcome
  structured_json TEXT,                                -- {subject_entity, predicate, value, unit, qualifiers{}} filled lazily
  scope TEXT NOT NULL DEFAULT 'store',                 -- store | doc:<ref> | finer
  valid_from TEXT, valid_to TEXT,                      -- world time, optional, closure via supersession (II.1)
  confidence_extraction REAL, meta_json TEXT,
  redacted_at TEXT, created_at TEXT NOT NULL,
  created_by_kind TEXT NOT NULL, created_by_ref TEXT);

-- reified derivations (AIF I/S-node split): claim produced FROM other claims
CREATE TABLE derivations (
  id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES claims(id),
  method TEXT NOT NULL,                                -- entailment|aggregation|calculation|generalization
  producer_kind TEXT NOT NULL, producer_ref TEXT,
  confidence REAL, rationale TEXT, created_at TEXT NOT NULL);
CREATE TABLE derivation_premises (
  derivation_id TEXT NOT NULL REFERENCES derivations(id),
  premise_kind TEXT NOT NULL,                          -- local|uri: cross-store premises are wb-truth:// URIs
  premise_ref TEXT NOT NULL,                           -- local claim id, or wb-truth://<store>/claim/<id>
  PRIMARY KEY (derivation_id, premise_ref));
-- no FK on premise_ref by design: premises may live in another store. The engine
-- fast-paths local refs, resolves uri refs through the registry at evaluation
-- time, and FAILS CLOSED (derived confirm blocked, needs_review finding) when a
-- cross-store premise is unresolvable or unconfirmed.

-- typed links (append-only, retraction via companion table)
CREATE TABLE claim_links (
  id TEXT PRIMARY KEY, from_claim_id TEXT NOT NULL REFERENCES claims(id),
  link_type TEXT NOT NULL,                             -- supports_span|about_entity|supersedes|conflicts_with|refutes|cites_external|relates_to
  to_kind TEXT NOT NULL,                               -- evidence_span|claim|entity|external_uri
  to_ref TEXT NOT NULL,                                -- record id or URI
  role_json TEXT,                                      -- conflict_type rebut|undermine|undercut, supersession_reason corrected|refined|valid_time_closed|source_retracted|preference_changed, support role quote|paraphrase
  target_fingerprint TEXT,                             -- Doorstop: sha256 of target content at link/review time
  fingerprint_reviewed_at TEXT,
  created_at TEXT NOT NULL, created_by_kind TEXT NOT NULL, created_by_ref TEXT);
CREATE TABLE link_retractions (
  link_id TEXT PRIMARY KEY REFERENCES claim_links(id),
  at TEXT NOT NULL, actor_kind TEXT NOT NULL, actor_ref TEXT, reason TEXT);

-- L4: the audited lifecycle (transaction time)
CREATE TABLE claim_status_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,               -- total order (house pattern: events/store.py). "Latest status"
                                                       -- is by seq, never by timestamp string alone
  id TEXT NOT NULL UNIQUE, claim_id TEXT NOT NULL REFERENCES claims(id),
  status TEXT NOT NULL,                                -- proposed|confirmed|rejected|expired|challenged|needs_review|superseded|retracted
  at TEXT NOT NULL, actor_kind TEXT NOT NULL,          -- human|agent_run|system
  actor_ref TEXT, basis_kind TEXT NOT NULL,            -- gesture|rule|import|sweep
  basis_ref TEXT, note TEXT);

-- L8: decision time, the human gesture on exact content
CREATE TABLE gestures (
  id TEXT PRIMARY KEY, at TEXT NOT NULL,
  surface TEXT NOT NULL,                               -- cli|dashboard|chat_consent|fold
  actor_ref TEXT NOT NULL,
  kind TEXT NOT NULL,                                  -- confirm|reject|edit_confirm|defer|scope
  subject_ref TEXT NOT NULL,                           -- the claim this gesture was minted FOR: a gesture is never
                                                       -- a bearer token usable against other content
  payload_sha256 TEXT NOT NULL,                        -- hash of the EXACT content shown
  payload_excerpt TEXT NOT NULL,
  context_sha256 TEXT,                                 -- hash of the receipts AS DISPLAYED (support quotes/links) at
                                                       -- mint time, NULL when none were shown: the gesture can attest
                                                       -- what evidence the human saw, not just the claim words
  expires_at TEXT,                                     -- freshness bound for deferred use (CLI --gesture), NULL = surface-immediate
  consumed_at TEXT);                                   -- single-use: set by the engine on first use, reuse refused

-- controlled mutation: redaction (anti-anchoring + privacy)
CREATE TABLE redaction_events (
  id TEXT PRIMARY KEY, subject_kind TEXT NOT NULL,     -- claim|evidence|span
  subject_ref TEXT NOT NULL, at TEXT NOT NULL,
  actor_ref TEXT NOT NULL,
  basis_kind TEXT NOT NULL,                            -- gesture|policy
  basis_ref TEXT NOT NULL,                             -- gesture id, or profile policy key
  reason TEXT NOT NULL);                               -- rejected_content|expired_content|privacy|source_takedown
-- basis rules: content that was EVER confirmed can only be redacted with a human
-- gesture. Never-confirmed content (proposed/rejected/expired) may be redacted by
-- profile POLICY (store.yaml, human-authored = standing consent), which is how
-- co-think's discard-by-default and proposal expiry work without a per-item prompt.

-- projections: render manifests for resident Markdown views (drift detection +
-- reconcile mapping). Rebuildable projection METADATA, exempt from the
-- append-only triggers (like claims_current): rows update in place on re-render.
CREATE TABLE projections (
  id TEXT PRIMARY KEY, path TEXT NOT NULL,             -- repo-relative target file
  rendered_at TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  manifest_json TEXT NOT NULL,                         -- [{claim_id, heading_path, quote, start, end}, ...]
  health TEXT NOT NULL DEFAULT 'clean',                -- clean|drifted|failed
  health_reason TEXT);

-- sweeps (staleness machinery, populated from K3)
CREATE TABLE sweeps (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL,             -- supersession|fingerprint|freshness
  at TEXT NOT NULL, params_json TEXT);
CREATE TABLE sweep_findings (
  id TEXT PRIMARY KEY, sweep_id TEXT NOT NULL REFERENCES sweeps(id),
  subject_kind TEXT NOT NULL, subject_ref TEXT NOT NULL,
  finding TEXT NOT NULL, resolved_at TEXT, resolved_by_ref TEXT);
```

Key indexes: `claim_status_events(claim_id, at DESC)`, `claim_links(from_claim_id)`, `claim_links(to_kind, to_ref)`, `claims(scope, claim_kind)`, `claims(scope, valid_from DESC)` (Cozo currency trick), `evidence(content_sha256)`, `evidence_spans(evidence_id)`.

**Invariant enforcement (not just library discipline):**
- **Canonical claim hash, defined:** `claims.canonical_sha256` = sha256 over canonical JSON (sorted keys, normalized whitespace) of `{proposition, claim_kind, structured_json, scope, valid_from, valid_to}`, computed by the engine at insert. It is what gestures bind to, what dedup keys on, and it survives redaction (the audit chain stays verifiable even after content is destroyed).
- **Append-only triggers:** `BEFORE UPDATE` / `BEFORE DELETE` on every base table `RAISE(ABORT, 'append-only')`, with exactly one carve-out: an UPDATE on `evidence`/`evidence_spans`/`claims` that only transitions `redacted_at NULL -> NOT NULL` while nulling content fields (`proposition -> '[redacted]'`, `structured_json/content/content_path/quote_exact -> NULL`). The WHEN clause compares every other column OLD-vs-NEW with `IS` (verbose but sound). Ids, hashes, links, and events are always retained.
- **Status machine (full):** `proposed -> confirmed | rejected | expired | retracted`. `confirmed -> challenged | superseded | retracted`. `challenged -> confirmed (re-affirm gesture) | superseded | retracted`. `needs_review` is an overlay state entered only by sweeps/rules and cleared by any human-gestured transition. `expired` is entered only by rule (per-profile `proposal_max_age`, and co-think session end), never requires a gesture. `superseded` requires a confirmed supersedes link. Duplicate transitions are idempotent no-ops (two surfaces confirming the same claim produce one event). Agents MAY challenge confirmed claims (challenge = status event + conflict edge + evidence, resolution is always human).
- **Confirm gate:** `confirmed` requires `actor_kind='human'` AND `basis_kind='gesture'` AND the gesture's `payload_sha256` equal to the claim's `canonical_sha256`. An agent-run actor on a confirm event is rejected at the engine layer (the AOV "AI cannot approve its own content" rule, structural).
- **Weakest link:** a derived claim cannot be confirmed while any premise is unconfirmed or unresolvable (cross-store premises resolve through the registry and FAIL CLOSED). Profile-overridable later only if a consumer proves the need.
- **Supersession closure semantics** (the Cozo transplant, made precise): `updated` and `preference_changed` close the predecessor's valid interval at the successor's `valid_from` (the engine REQUIRES `valid_from` on the successor for these reasons). `valid_time_closed` closes at the successor-supplied date with no content change. `corrected` VOIDS the predecessor's interval (it was never true) rather than closing it. `refined` inherits the predecessor's interval (same fact, better words). `source_retracted` voids support and routes dependents to `needs_review`. The projection renders intervals accordingly.
- **Single confirmed successor:** confirming a claim that supersedes X runs inside `BEGIN IMMEDIATE` and verifies no other confirmed supersedes-X link exists. A second competing supersession lands as `needs_review` conflict, never a silent branch. All propose/confirm/supersede ops use `BEGIN IMMEDIATE` (two-process topology: gateway + sidecar sweeps).
- **Dedup by canonical hash:** proposing a claim whose `canonical_sha256` matches a live (non-terminal) claim returns the existing claim instead of inserting a duplicate.
- **Redaction co-status:** redacting a claim also appends a terminal status event (`retracted`, basis = the redaction) unless already terminal, so `claims_current` can never show a confirmed "[redacted]" ghost.
- **Blob refcounting:** redaction nulls the row's `content_path`, then deletes the blob file only when no live evidence row still references that `content_sha256`. Shared blobs with one redacted and one live reference keep the bytes (the live row's owner chose to keep them), documented behavior.
- **Fingerprint scope:** `target_fingerprint` is meaningful only for MUTABLE targets: `about_entity` (registry descriptions), `cites_external` (web drift), and future document-side links from living artifacts. For immutable targets (`supports_span`, `supersedes`), staleness comes from status queries, not content drift, and the engine leaves the fingerprint NULL. (This demotes the Part I §8 "strongest single steal" enthusiasm to its correct scope.)

**Authorship and AI-failure invariants (second devils-advocate round, §II.10b):**
- **Gestures are single-use, bound, and fresh:** a gesture is minted FOR one claim (`subject_ref`), is consumed on first use (`consumed_at` set, reuse refused, plus a partial UNIQUE on gesture-based confirm events as belt and braces), and deferred use (`--gesture` from the CLI) honors `expires_at`. An old gesture can never confirm a re-proposed or different claim. Replay is structurally dead.
- **Producer identity is captured at write time or the write is refused:** any propose/capture with `actor_kind='agent_run'` MUST carry documented `meta_json` keys `{model, harness, surface, session_id, call_id?}`, validated by the engine (queryable via `json_extract`, promotable to real columns in a v2 migration). The ledger self-carries the durable minimum precisely because session transcripts and the inference-provenance JSONL (when built) age out on TTLs. "Which model authored this claim" must be answerable from the ledger alone, forever, or model-level recall ("model X confabulates, flag its claims") is impossible.
- **Trust classes have laws:** `trust_class` is assigned by the engine from acquisition context, never accepted from agent callers (`user_authored`/`user_curated` can only originate from human surfaces). Chat transcripts and mixed documents resolve authorship at the SPAN (`evidence_spans.author_kind`), where support actually attaches. A claim whose only support is `agent_authored` content is flagged as such in the confirm dialog (the human sees they are ratifying an AI's word against an AI's writing). A claim supported ONLY by `external_quarantined` evidence cannot be confirmed without an explicit override recorded on the gesture (the problem-map injection rule, enforced).
- **The ledger's own exhaust cannot launder back in:** `materialize.py` stamps every projection it writes (front matter carrying the source `store_id`), capture detects the stamp and sets `evidence.derived_from_store`, and store-derived evidence never counts toward corroboration and never serves as sole support (its support links carry role `projection`). This kills the availability cascade INSIDE the system: claims cannot cite the store's own output as independent confirmation.
- **Refuted claims resist re-entry:** dedup-by-hash catches identical re-proposals, and paraphrases are screened at propose time against confirmed negative claims (`refutes`-linked negations) in scope: retrieval-shortlist from K3 (existing embedding search), NLI scoring from K4 (HHEM pre-filter). Matches are never silently blocked, they land as `needs_review` proposals with the refutation cited, so the human sees "you already ruled this false" instead of re-reviewing from scratch.

**Rejection is reason-classed (Q4 resolution: "it depends" made structural).** Kaden's distinction: denying an edit because you dislike it is ephemeral, while saying "that is not true" is load-bearing knowledge future agents need. The reject gesture therefore carries a reason class that decides durability:
- `reject_as_false`: the rejection MINTS a negative claim ("It is not the case that P"), confirmed by the SAME gesture (the human just attested exactly that, on exactly that content), with a `refutes` link to the rejected claim. The rejected proposal's row is then redacted per policy, but the confirmed negation persists with full text. Falsehoods become first-class, citable, verifiable facts.
- `reject_as_preference`: the flow offers to mint a preference claim ("I prefer X not be done") into the appropriate store, confirmed by the same gesture if accepted.
- `reject_plain` (dislike, off-target, duplicate): status `rejected`, content redacted per the profile's `rejected_content` policy (default remains redact-immediately). Nothing durable beyond the tombstone chain.
This preserves anti-anchoring (unratified proposal text still dies) while capturing ratified negations and preferences, which are the OPPOSITE of unearned authority: they are the most explicitly human-attested content in the system.

**Projections (rebuildable, never truth):** `claims_current` (latest status per claim + effective valid interval derived Cozo-style along `supersedes` chains + `health`/`health_reason` columns lifted from Proof's projection pattern). Materialized as a table rebuilt by the engine, never written by consumers.

**Three clocks, explicitly mapped:** valid time = `claims.valid_from/valid_to` + derived closures. Transaction (belief) time = `created_at` on rows + `claim_status_events` ordered by `seq` (total order, never by timestamp string). Decision time = `gestures.at` (a batch-reviewed confirmation records when the human actually decided, distinct from when the status event landed). This satisfies the co-think three-clock requirement without a third column set.

**Migration contract (schema evolution across a fleet of live stores). Committed 2026-07-11 after Kaden's future-proofing question.** The premise is that schema v1 WILL need changes: the contract makes changes cheap and safe rather than pretending they will not happen.

1. **Per-store versioning, migrate-on-open.** Every store carries `schema_version` (store_info + `PRAGMA user_version`). Opening a store runs pending forward migrations, the proven house pattern (`entities/migrations.py` runs on every connection). No store is ever manually migrated, and a half-updated fleet is a non-event: each store upgrades the moment anything touches it. A registry-driven `wbuddy truth migrate --all` exists for proactive sweeps, and the engine REFUSES stores with a newer version than itself (already committed) so an old engine can never corrupt a new store.
2. **Snapshot before any version bump.** A version-bumping migration first copies `store.db` aside (`store.pre-v<N>.db`, pruned after the next successful backup cycle). Rollback from a bad migration is file restoration, not archaeology.
3. **Migrations are append-only too.** A migration may add tables, columns, indexes, and triggers, backfill new fields, and rebuild projections. It may NEVER drop or rewrite ledger content (the same invariant the triggers enforce on DML, honored at DDL time by review discipline plus the fixture suite below). Reinterpretation of old rows happens in projections and queries, not by editing history. The designed evolution path for deliberately-soft fields (the producer-identity and basis meta keys) is exactly this: promote a documented `meta_json` key to a real column with a backfill, old rows keep their data, nothing is invalidated.
4. **Frozen fixture stores per released version.** The test suite keeps one small frozen store file per schema version ever released. Every new migration must open and correctly upgrade EVERY prior fixture, and the three workload fixtures re-run on the result. A migration that strands old data cannot merge.
5. **The JSONL export is the version-independent escape hatch.** `export/claims.jsonl` carries a `format_version` header record, `truth_store_import` upcasts any OLDER format on import, and the format itself only ever gains fields. Because the export is committed to git, every store has a lossless, human-readable, historically-versioned text form that can rebuild a working store even across a hypothetical v1-to-v5 jump, or feed a full rewrite if the engine were ever abandoned. This is the second, independent recovery path: SQLite snapshots protect against bad migrations, the JSONL protects against everything else.
6. **Documents can never be stranded.** Resident Markdown projections are regenerated from the store, so a schema change is followed by a re-render, and the files update themselves. Their front-matter stamp (store_id + generated notice) is deliberately minimal and version-stable. Class-2 expression tables, when they arrive, ride the same per-store migration train.
7. **Profiles never invalidate history.** `store.yaml` constrains NEW writes only. Tightening a profile (new required field, removed claim kind) affects proposals from that moment forward, and existing rows remain valid history: there is no such thing as a retroactively-illegal claim.
8. **Identity is permanent.** Record ids and `wb-truth://` URIs never change across migrations (invariant 9), so cross-store references, committed JSONL files, and any external notes citing a claim survive every schema version.

## II.6 Engine, module layout, capabilities, gates (committed)

```
work_buddy/truth/
  __init__.py
  migrations.py      # PRAGMA user_version framework, v1 = II.5 DDL + triggers
  store.py           # open/create, connection discipline (WAL, busy_timeout), append ops
  lifecycle.py       # status machine + gesture verification
  anchors.py         # WA selectors, AOV-ported re-anchor + quote firewall
  fingerprints.py    # Doorstop-style link fingerprints
  queries.py         # current/as-of/conflicts/needs_review + sweeps
  profiles.py        # store.yaml load + propose/confirm validation
  registry.py        # truth_stores registry
  redact.py          # sanctioned redaction op
  materialize.py     # claims_current rebuild + Markdown projections (K2+)
  prompts/           # K3/K4: attributed ported prompt text
  export_prov.py     # K5
work_buddy/mcp_server/ops/truth_ops.py
knowledge/store/truth/*.md          # capability declarations + profile directions (K1/K2)
```

**Capability surface (K1):** `truth_store_create`, `truth_store_list`, `truth_evidence_capture`, `truth_span_mark`, `truth_claim_propose`, `truth_claim_confirm`, `truth_claim_reject`, `truth_claim_challenge`, `truth_claim_supersede`, `truth_claim_redact`, `truth_query`, `truth_sweep`, `truth_export_prov` (K5), and from K2: `truth_materialize`, `truth_projection_reconcile` (the drift flow in §II.4's projection contract).

**Gate mechanics.** `truth_claim_confirm`/`reject`/`redact` use a **no-grant consent variant**: per-invocation prompting with NO time-window caching of approvals ("Allow for 15 min"/"Allow always" must not exist for these ops, because a cached grant would auto-approve future claims no human saw while the engine mints gesture rows asserting they did). If the consent substrate lacks a no-grant flag today, K1 adds it, and this is a K1 exit criterion. The dialog payload is composed **server-side from the claim row** (never from agent-supplied parameters), and the minted `gestures.payload_sha256` is computed from that server-composed content. `user_initiated()` surfaces (dashboard click, CLI review session, fold) remain the primary gesture mints. Workflow grants never cover confirms. `truth_claim_propose` and `truth_evidence_capture` are normal-weight (agents propose freely, nothing becomes true). Store creation runs through workflow consent.

**Batch review semantics (the fold rule).** Batch review NEVER means one gesture covering N claims. It means one sitting in which each claim is individually rendered and individually marked (AOV fold semantics: silence = abstain, ambiguity clarified not guessed), and applying the marks mints **one gesture row per marked claim**, each carrying that claim's own `canonical_sha256`. Throughput comes from the reading-and-marking flow, not from skipping per-item gestures. A "confirm all" button that hashes a batch payload is structurally invalid (no gesture would match any claim's hash, by construction). **Stale-view protection:** each submitted mark carries the `canonical_sha256` the client DISPLAYED, and the server rejects any mark whose displayed hash no longer matches the claim's current hash (the claim changed or was superseded since render), so a gesture can never ratify content the human did not actually see.

**Raw-file honesty.** A local user (or rogue tool call) can always open `store.db` with sqlite3 directly. The triggers make casual violations fail, an integrity-check op (`truth_sweep kind=integrity`) detects the rest (hash mismatches, status events without gestures), and the store remains user-owned by design. The gate is enforced against agents at the capability layer (the only layer agents get), not against the owning human.

## II.7 Phase plan with exit criteria

Each phase lands through `/wb-dev-pr` (tests, chained doc-update, PII scan, DCO sign-off), Kaden merges. Live testing per `/wb-dev-live-testing` where a phase touches the gateway. The parallelized execution layer over these phases (orchestrator/builder roles, waves, work packages, joins, human gates) is [implementation-dag.md](implementation-dag.md).

| Phase | Builds | Tests (written with the phase, not after) | Exit criteria |
|---|---|---|---|
| **K0: schema + engine core** | migrations v1 (DDL + triggers), store.py, lifecycle.py, anchors.py (AOV port), fingerprints.py, queries.py (current/as-of), profiles.py, redact.py, export/import (JSONL) | unit: append-only trigger enforcement (UPDATE/DELETE raise), redaction carve-out shape + co-status + blob refcount, canonical-hash computation and gesture binding (mismatch rejection), status-machine transitions incl. rejected/expired paths, agent-confirm rejection, duplicate-transition idempotence, expiry-by-rule + policy redaction of never-confirmed content, weakest-link rule incl. cross-store premise fail-closed (registry stub), single-confirmed-successor race under BEGIN IMMEDIATE, seq total-ordering of status events, dedup-by-canonical-hash, anchor re-anchor + quote firewall, fingerprint scope (NULL on immutable targets), as-of + derived-interval queries per supersession-reason semantics, JSONL export/import round-trip preserving store_id (with `format_version` header + older-format upcasting), gesture single-use + binding + expiry (replay refused), producer-identity enforcement (agent write without model/harness keys refused), trust-class assignment laws (agent-supplied user_authored refused), span-level author resolution on mixed evidence, quarantined-sole-support confirm override, store-derived evidence excluded from corroboration, and the migration contract exercised end to end (a frozen v1 fixture store + a synthetic v2 migration: migrate-on-open upgrades it, pre-migration snapshot appears, all workload fixtures still pass on the upgraded store, newer-version refusal fires). **Fixture walkthroughs: the three workloads** (an electricrag canon file with sources + one supersession sweep, a my-career fact set with confirmation + a derived bullet artifact, a co-think session slice with span-first claims + micro-canonization + expiry + discard-by-default) each expressed end-to-end through the engine API | pytest green, all three fixtures express without schema bending, zero schema TODOs left in code |
| **K1: gateway + CLI surface** | truth_ops.py + capability declarations, `wbuddy truth` CLI verbs (same engine ops, §II.4b: propose/capture/query for anyone, confirm only interactive-TTY or `--gesture`), registry.py, consent wiring incl. the **no-grant consent variant** (per-invocation, no TTL grants, server-composed dialog payload), events spine emissions, backup-registry hookup, MCP restart + `wb_search` discovery | unit: op param validation, consent-weight declarations, no-grant variant behavior. live: propose from a fresh session, confirm via consent prompt (gesture minted from server-composed payload), a 15-min grant demonstrably NOT covering a second confirm, agent self-confirm rejected live, `truth.*` events observed, store appears in backup coverage, registry rejects duplicate store_id | all capabilities callable from a clean session, gate + no-grant behavior demonstrated live, docs units authored via chained dev-document |
| **K2: first real stores + consumer onboarding** | `person-facts` profile + `project-canon` profile (store.yaml + directions units), **`wbuddy project init`** (§II.4b: scaffold store, register, generate .gitignore + the ~20-line consumer rules snippet), electricrag and my-career onboarded via init, canonize-protocol-through-capabilities walkthrough on real content (new canonizations only, §9 Q2), materialize.py maintaining `docs/canon/*.md` as projections in their natural location (every projection stamped with its source store_id, and capture honors the stamp: the anti-cascade rule live), the full projection contract (§II.4): render manifests, drift detection, `truth_projection_reconcile` mapping human file edits to supersession proposals | live walkthrough scripts, projection rebuild test, drift fixtures (hand-edit a rendered canon file: assertion edits become supersession proposals mapped to the RIGHT claims, typo edits reconcile silently, dirty files are never overwritten), init idempotence, throwaway-data protocol for any mutation test | each store holds a real confirmed claim chain including one supersession with sweep findings, canon Markdown projection regenerates losslessly, and a fresh consumer-repo session carrying ONLY the init-generated snippet proposes a sourced claim via CLI |
| **K3: review view + staleness** | **Truth review view in the React dashboard** (dashboard-react/, per t-af909c0d constraints: same-origin `/api/truth/*` routes on the dashboard service, reuse `/api/state` + `/api/events` SSE, no new runtime deps, palette sync). Fold semantics in UI: per-item marks (confirm / reject-as-false / reject-as-preference / reject / defer / edit-then-approve), apply-marks mints **one gesture per marked claim** with stale-view hash checks. **Dirty-state as first-class** (unsubmitted marks tracked, route-change/close guard, localStorage draft retention), per the task note's saved-vs-unsaved directive. Components decomposed per the composable-views north star (ReviewQueue, ClaimCard, MarkBar, EvidencePeek, DiffView) and recorded in `dashboard-react/COMPONENTS.md`. Plus: fingerprint + supersession + freshness sweeps as sidecar jobs, mem0-derived dedup proposal prompt (propose-only), the refutation re-entry screen (retrieval shortlist against confirmed negations at propose time), redlines-rendered diffs, redaction policy live per profile. Interactive CLI review demoted to optional headless follow-up. **Surface-layering note (Kaden, 2026-07-11): this review queue is deliberately the SIMPLEST truth surface, a queue, not an editor. It is level one of three: (1) this review queue, (2) a document-anchored truth view (a document with its claims, verification results, and citations in context: built as K5 wave 5B per the implementation DAG, first version locked 2026-07-11, still not co-think), (3) co-think itself, the co-thinking layer (provocations, Socratic dispatch, live CRDT editing), which is a level ABOVE truth recording and consumes the same stores. Do not let level-3 ambitions leak into this build** | unit: sweep correctness, per-item gesture minting (N marks = N gesture rows with N distinct hashes), stale-view rejection, dedup proposal never auto-applies, Flask-test-client route tests (never bind live ports, skip-with-hint when dist absent). live: review 20 proposed claims in one sitting via marks, measure gesture throughput, verify dirty-guard on tab redirect | review throughput measured and acceptable to Kaden (explicit check-in), staleness findings actionable, gate integrity preserved under batching, component inventory started |
| **K4: verification stack** | own mini design doc first (gate: must address drafter-independent retrieval for the verifier, so checker and drafter do not share one blind spot), then: Claimify-recipe decomposition, LettuceDetect + HHEM classifier-inference path (transformers/ONNX local, not LM Studio), Graphiti-derived contradiction prompts + ported invalidation, NLI upgrade of the refutation re-entry screen, citation-integrity check consuming spans (cognitive-dangers profile). Generated artifacts are class-1 projections (§II.4 document classes): their line-to-claim citations live in the render manifest, and verification checks each manifest entry (the claim must entail the line) | golden-set tests for decomposition, entailment scoring against fixtures, contradiction proposals land as needs_review never auto-resolve, paraphrased-refutation fixture caught by the screen | a generated artifact (resume section or canon paragraph) machine-checked against confirmed claims with violations routed to review |
| **K5: federation + exports + document truth view** | consolidated-index partition per registered store (claims + evidence embedded by the existing embedding service, per-partition freshness crons, consumer opt-in flag, warming-signal cold-start behavior: all the proven index-consolidation machinery), cross-store search surface, PROV-JSON export, optional DuckDB rollup reads, co-think integration spec handed to the co-think phasing, and the **level-2 document truth view** (first version locked 2026-07-11: class-1 generated docs only via render manifests, read-mostly, four passage status treatments, detail rail reusing K3 components, actions on claims never prose, re-render gated on reconciled drift) | index round-trip tests, freshness-cron self-skip behavior, export validates against PROV-JSON schema, doc-view route + component tests | two stores searchable in one query, partitions refresh unattended, export consumed by an external PROV tool, doc view proven on a real canon file incl. the end-to-end drift flow |

Explicitly deferred beyond K5: the co-think editor build (its own phased plan), twin views, extraction automation at scale, nanopub signing, any dashboard beyond the review queue.

## II.8 Risks and mitigations

| Risk | Mitigation |
|---|---|
| Schema wrong despite fixtures | Append-only + rebuildable projections make most errors reinterpretations. Only the invariant list is unforgivable, and triggers enforce it mechanically |
| Schema evolution strands live stores or their documents | The migration contract (§II.5): migrate-on-open per store, pre-migration snapshots, append-only migrations, frozen fixture stores per released version, version-headed JSONL as the independent escape hatch, regenerable documents, permanent ids |
| Review throughput too low (queue rots) | K3 measures it explicitly with a Kaden check-in as exit criterion. Dedup proposals + batch fold gestures + per-profile gate policies are the levers |
| Anchor rot on living documents | Snapshot evidence is immutable (offsets safe). Living-doc spans re-anchor via quote+prefix/suffix with the AOV firewall, failures land as needs_review, never silent |
| Redaction vs auditability tension | Redaction nulls content but never ids, `canonical_sha256`, links, or events, so gesture bindings stay verifiable after content destruction. Redaction co-appends a terminal status (no confirmed ghosts) and refcounts shared blobs. Problem-map invariant 1 carries an explicit sanctioned-redaction exception (amended). Whole-store deletion remains the nuclear option |
| Single-machine canon (gitignored store.db does not travel with clones) | Committed JSONL export (`export/claims.jsonl`) is the diffable, repo-travel form, `truth_store_import` rebuilds the working store, registry rejects duplicate store_id on restore-beside-live. Concurrent multi-machine WRITES stay out of scope for v0 (documented) |
| Gate bypass via raw SQLite | Triggers + integrity sweep + capability-layer enforcement against agents. The owning human is not the adversary |
| Cross-process contention on one store | WAL + busy_timeout (house standard), per-scope stores shard writes naturally, single-writer worst case is one store not the system |
| Blob growth | Content-addressed blobs dedup by hash, size ceiling per profile, big media stays out of scope for v0 |
| Consumer repos without work-buddy | Stores are plain SQLite + YAML, readable by anything. The engine is required only for writes that must honor invariants |
| Human edits a resident Markdown projection (drift) | The projection contract (§II.4): render manifests + hash-based drift detection, materialize never overwrites dirty files, reconcile maps edits to supersession proposals through the review queue with honest `unattested` attribution |
| Second-store-of-truth drift (registry vs knowledge store) | The non-competition table (convergence.md §3) is enforced in reviews: no truth jobs in the entity registry, no claim tables outside `.wb-truth/` |
| Naming churn after code lands | §9 Q3 asks for ratification before K0 merges, rename is one mechanical sweep at most |

## II.9 Forks: RESOLVED by Kaden (2026-07-11)

| Q | Decision | Where it landed |
|---|---|---|
| Q1 co-think sidecar | **(A) Unified**: claims/gestures/supersessions live in the scope's `.wb-truth/` store (profile `cothink-doc`), `.wb-cothink/` keeps only CRDT runtime state referencing claim ids | §II.4, co-think phasing consumes this |
| Q2 electricrag scope | **(A) New canonizations only**, opportunistic per-topic migration when a file is next touched | K2 row |
| Q3 naming | **`truth` ratified**, and the question expanded into the consumer-surface design: CLI-first invocation, `wbuddy project init` onboarding, canon files remain materialized projections in natural locations, no wb MCP in consumer repos | §II.4b (new) |
| Q4 rejected-content | **Reason-classed** per Kaden's distinction: reject-as-false mints a confirmed negative claim (refutes link), reject-as-preference offers a preference claim, plain reject redacts per policy | §II.5 rejection block |
| Q5 review surface | **(B) Dashboard first, in the REACT app** (not the legacy HTML dashboard), honoring t-af909c0d's constraints (same-origin `/api/*`, component decomposition + COMPONENTS.md inventory, dirty-state guarding, no new runtime deps). CLI interactive review demoted to optional headless follow-up | K3 row |
| Q6 JSONL committing | **Committed by default for all stores** (private repos, versioning wanted), profile opt-out retained for future public/shared repos | §II.4 |

Kaden also requested a narrative, usage-first description before proceeding: [layman-description.md](layman-description.md).

## II.10 Devils-advocate findings (run 2026-07-11, Fable subagent, mandate: discredit)

Twelve objections returned, every CRITICAL and SIGNIFICANT claim re-verified against the plan text and house code before acting. Verdict: **needs amendment** (architecture holds, schema and gate mechanics had real holes). All amendments are now baked into §II.4-II.8 above. The record:

| # | Severity | Objection (verified?) | Resolution |
|---|---|---|---|
| 1 | CRITICAL | Canonical content hash undefined, unstored, unrecomputable after redaction (verified) | `claims.canonical_sha256` added, definition written into §II.5, survives redaction |
| 2 | CRITICAL | Batch gestures contradict per-claim hash rule (verified) | Fold rule made explicit: per-item marks mint per-item gestures, confirm-all structurally invalid (§II.6) |
| 3 | CRITICAL | `rejected`/`expired` missing from status enum, expiry required a gesture it cannot have, challenge transitions unspecified (verified) | Enum extended, full transition table written, expiry is rule-based, policy redaction covers never-confirmed content, agents may challenge confirmed claims (§II.5) |
| 4 | CRITICAL | Cross-store derivation impossible with local-FK premises (verified, with one nuance: resume bullets are composition artifacts, not claims, but cross-store derived claims remain legitimate for twin/preference flows) | `derivation_premises` takes local ids or `wb-truth://` URIs, fail-closed resolution (§II.5) |
| 5 | SIGNIFICANT | Cozo transplant underspecified: `updated` not in enum, closure-per-reason undefined, branching successors, no write serialization (verified) | Supersession-semantics rules written (close/void/inherit per reason), successor `valid_from` required for closing reasons, single-confirmed-successor check under `BEGIN IMMEDIATE` (§II.5) |
| 6 | SIGNIFICANT | Chat-consent 15-min/24h grants would auto-approve unseen claims (verified against consent docs) | No-grant consent variant committed, server-composed dialog payload, K1 exit criterion includes proving a grant does NOT cover a second confirm (§II.6, §II.7) |
| 7 | SIGNIFICANT | Redaction violates problem-map invariant 1, confirmed-ghost claims, blob refcount missing (verified) | Invariant 1 amended in problem-map with the sanctioned-redaction exception, redaction co-status rule, blob refcounting (§II.5, §II.8) |
| 8 | SIGNIFICANT | Doorstop fingerprint mis-transplanted onto immutable targets (verified) | Fingerprint scoped to mutable targets only (entities, external URIs, living artifacts), NULL elsewhere, Part I enthusiasm demoted (§II.5) |
| 9 | SIGNIFICANT | No total-order column despite the house pattern having one (verified against `events/store.py`) | `claim_status_events.seq` AUTOINCREMENT added, latest-status defined by seq (§II.5) |
| 10 | SIGNIFICANT | Project canon becomes single-machine, no restore story, store_id duplication on restore (verified) | Committed JSONL export as the diffable repo-travel form + `truth_store_import` + registry duplicate-store_id rejection, multi-machine writes documented out of scope, escalated as §9 Q6 (§II.4) |
| 11 | SIGNIFICANT | `trust_class` cannot express unattested-by-default (verified) | `unattested` added and made the default for pre-existing files (§II.5) |
| 12 | NITPICK | Open claim-kind set vs enum, dangling entity refs, engine-version guard, redlines license check, pre-K3 duplicate proposals (verified) | claim_kind documented as profile-constrained open set, integrity sweep gains dangling-ref check, engine refuses newer-schema stores, redlines license verified at adoption time, dedup-by-canonical-hash handles duplicates from K0 |

What survived attack unchanged: the append-only trigger carve-out, WAL two-process topology, Avenue B closure, all license calls and no-import stances, the targeted-store architecture, and the phase ordering.

### II.10b Second round: authorship provenance and AI-failure coverage (2026-07-11, Fable, on Kaden's question)

Focused mandate: discredit the claim that K0's authorship-provenance and AI-failure-mode coverage is a sufficient foundation for co-think. Ten objections, verdict **needs amendment**: the deferral of document-span attribution, per-span AI basis, and interaction provenance to co-think's phase was confirmed SAFE (additive later), but three things had to land in K0 and five had to be named now. All verified against the plan text before acting, all baked in above:

| # | Severity | Objection | Resolution |
|---|---|---|---|
| 1 | CRITICAL | `trust_class` had no way to say "an AI wrote this": the laundering channel (agent text → unattested evidence → extraction → confirmation, human never told) | `agent_authored` + `mixed` classes, span-level `author_kind/author_ref` on `evidence_spans`, agent-sole-support flagged in the confirm dialog (§II.5) |
| 2 | CRITICAL | Gestures were eternal multi-use bearer tokens: an old gesture could confirm a re-proposed claim with a reproduced hash, no human in the loop | `subject_ref` binding + `consumed_at` single-use + `expires_at` freshness + partial-unique on gesture-based confirms (§II.5) |
| 3 | CRITICAL | Producer identity never captured: `agent_run` refs join stores that age out on 14-day TTLs, and NO field recorded which model authored a claim | mandatory producer-identity meta keys `{model, harness, surface, session_id, call_id?}` on every agent write, engine-refused otherwise. The ledger self-carries what it needs forever (§II.5), inference-provenance demoted to optional-join in §II.3 |
| 4 | SIGNIFICANT | §II.2 overstated the Proof-fields lift (basis/typed_by/model were nowhere) | honest mapping written into §II.2: basis = enforced meta key now, column in v2, span-level fields = co-think's additive table |
| 5 | SIGNIFICANT | The gesture attested the claim words, never the receipts shown | `context_sha256` on gestures (hash of displayed support at mint, NULL historically), review surface shows receipts by default for sourced claims |
| 6 | SIGNIFICANT | `trust_class` had no assignment laws, quarantine gate uncommitted | engine-assigned classes, agent-supplied user_authored refused, quarantined-sole-support requires explicit gesture override (§II.5) |
| 7 | SIGNIFICANT | The ledger's own projections could launder back in as "independent" evidence (internal availability cascade) | projection stamping + `derived_from_store` + excluded from corroboration and sole support, live at K2 (§II.5, K2 row) |
| 8 | SIGNIFICANT | Nothing stopped paraphrases of refuted claims (layman promise unbacked) | refutation re-entry screen: retrieval shortlist K3, NLI K4, matches land as needs_review citing the refutation (§II.5) |
| 9 | NITPICK | Interaction provenance had no declared durable home | co-think's event log named as that home in its phase, kernel refs carry producer identity per objection 3 |
| 10 | NITPICK | Verifier retrieval independence | named as a mandatory topic in K4's design-doc gate |

Confirmed safe as deferred: document-span attribution tables, per-span basis and review state, the co-think event log. The kernel's job is that nothing an agent does between now and then writes a record the editor's stronger provenance would contradict.

---

*Provenance: Part I drafted 2026-07-10 (analysis), Part II committed same day on Kaden's instruction, incorporating his corrections (targeted stores, entity-registry caution, Hindsight departure, license-rejection scope) and the completed Cozo study + Proof SDK source read. Agent-authored. §9 forks await Kaden. Not canon until red-penned.*
