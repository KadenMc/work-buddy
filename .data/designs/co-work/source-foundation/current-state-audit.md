# Current-state audit

**Audit point:** Current `main` after merged PR #269, 2026-08-09.

The exported conversation was written against an earlier point in the Co-work
work. This audit distinguishes reusable foundations on current `main` from
historical claims in the conversation.

## Executive finding

Work Buddy has several good domain-specific pieces but no shared source layer.
The missing abstraction is not a generic blob store. It is the combination of
stable source occurrence identity, exact retained content, trusted provenance,
authorized resolution, append-only observations, redaction coordination, and
reliable downstream delivery.

The most urgent correctness issue is present in the newest AI Truth path:
staging and human gating are strong, but the final canonical write can still
make an AI-formulated proposition appear human-created. More frontend
investment should wait until the actor/provenance model is corrected.

## Truth

### What exists

- Schema and export format v8 in `work_buddy/truth/migrations.py`.
- Separate evidence, evidence span, claim, derivation, claim-link, lifecycle,
  and review records.
- Evidence records retain a locator, content digest/blob, acquisition actor,
  and trust classification.
- Evidence spans retain selectors and exact quotes separately from claims.
- Claim identity is not changed merely by adding a source.
- `claim_links.role_json` is an extensibility seam for typed claim–evidence
  metadata, though current lifecycle behavior only understands its existing
  support conventions.
- The AI-assisted Truth flow in `work_buddy/cowork/truth_analysis*.py` already
  has valuable safeguards:
  - exact frozen action snapshots;
  - bounded context and research;
  - durable private candidate staging;
  - server-side selector, quote, identity, and payload validation;
  - human-only canonical decision;
  - idempotent candidate decisions; and
  - atomic claim/expression/evidence connection at the final Truth boundary.
- Co-work feedback capture is a useful narrow prototype: a human feedback
  action becomes a durable conversation message and `USER_INPUT` evidence.

### Gaps

- `work_buddy/truth/locators.py` validates locator syntax but intentionally does
  not resolve a source. `wb-session://...` is provider-unqualified.
- Truth trust/authorship is derived substantially from the caller. The public
  agent evidence capability uses an `agent_run` acquisition actor, while the
  generic span operation correctly refuses to let that agent assert a human
  author. There is no resolver-backed kernel path between those constraints.
- Current AI Truth candidate commit passes the human decision actor through
  `truth_surface.connect_claim`, which then proposes the claim with that human
  actor. Run/model metadata survives, but canonical authorship collapses AI
  semantic production into human creation.
- Claim–evidence relationships are not validated against one accepted schema. The
  current profile model also has no explicit claim-kind support policy.
- Claim confirmation permits no-support claims by compatibility behavior. That
  may be valid for some claim kinds, but it is not yet a deliberate per-profile
  rule.
- The low-level evidence/span/claim operations are not an atomic
  source-to-claim composite and can leave partial intermediates if orchestrated
  naively.
- Truth export has retained snapshots but no durable record of which resolver
  and resolver version produced a source, what native revision was observed,
  or how current availability differs from capture-time integrity.
- Feedback capture crosses conversation, Truth, and event stores without a
  source-owned outbox; it creates no evidence span over the utterance.
- Candidate addition/connection and claim lifecycle confirmation are not yet
  modeled as the deliberately separate human decisions the shared foundation
  requires.

### Current Truth capability disposition

The new composite does not scrap the Truth capability system. It narrows the
role of its public low-level writes and composes the store/kernel underneath
them so a crash cannot leave a half-built source-backed claim.

| Current capability/path | Disposition | Foundation change |
|---|---|---|
| `truth_store_create`, `truth_store_list` | Keep | Adopt the new schema/export versions and health reporting; source semantics do not belong here. |
| `truth_evidence_capture` | Keep as explicit low-level acquisition primitive | Continue recording agent-acquired/caller-supplied evidence conservatively. It must not claim that a locator was resolver-verified or act as the source-backed composite. |
| `truth_span_mark` | Keep as low-level/manual primitive | Preserve its strict anti-forgery rule. Agents cannot use it to manufacture human span authorship; the new resolver-backed kernel has a separate internal path. |
| `truth_claim_propose` | Keep for genuinely agent-produced standalone proposals | Preserve `scope`, `valid_from`, and `valid_to`; migrate new support writes to the validated claim–evidence relation. It is not called as one step in a public multi-call composite. |
| `truth_claim_confirm`, `truth_claim_challenge`, `truth_claim_reject`, `truth_claim_supersede`, `truth_claim_redact` | Keep, but extend | Use the persistent/bound human actor, keep candidate decisions separate, include new source/provenance state in review fingerprints, and write the Truth-owned projection outbox for eligibility changes. |
| `truth_query`, `truth_sweep` | Keep, but extend | Project multi-actor provenance, scope/valid time, new evidence relations, source attention/redaction, and Hindsight reconciliation state without guessing legacy fields. |
| Co-work Truth analysis get/search/fetch/submit and candidate commit | Keep as constrained/internal workflow, refactor commit | Route pre-egress source content through source-aware dispatch; bind output to the run/disclosure; replace or disable the collapsed-provenance canonical writer. |
| `truth_claim_propose_from_source` | New composite | Resolve/reserve/stage internally, execute one Truth transaction, then acknowledge/reconcile usage. It never orchestrates the public low-level capabilities over separate commits. |

No public lifecycle capability is removed merely because a higher-level UX uses
the new composite. The old AI candidate commit is the one path that cannot stay
enabled unchanged once the corrected schema begins accepting writes.

## Conversations and transcripts

### What exists

- `work_buddy/conversations/store.py` has stable conversation/message IDs,
  exact stored content, role, timestamps, and idempotent insertion.
- Transcript sessions carry provider, harness, session, and native-session
  identity.
- A provider registry already exists under `work_buddy/transcripts/` for
  Claude Code and Codex transcript projections.

### Gaps

- Legacy conversation rows do not establish which trusted ingress principal
  vouched for a `user` role. That role is useful evidence, not automatically
  verified local-human identity.
- `TranscriptTurn` has no stable native item ID or exact raw-record digest.
- Current adapters normalize for reading: they strip text and can join blocks.
  That is appropriate for browsing/analysis but not exact source authority.
- Transcript provider IDs (`claudecode`, `codexcli`) intentionally differ from
  execution provider IDs (`claude-code`, `codex`). A source-provider namespace
  must not conflate them.
- Turn indexes, content search, and “latest message” are not stable source-item
  identity and must not enter `SourceRef`.
- No trusted current-turn bridge captures an exact prompt before harness
  normalization, delayed transcript flush, or TTL deletion and injects a
  prompt-scoped retained reference into the agent context.

## Journal

### What exists

- `dashboard-react/src/apps/journal/contracts.ts` already models exact,
  unnormalized input (`exactText`), `clientMutationId`, and separate persistence
  versus processing state.
- Its capture command is already specific: `target_id` is
  `auto | log | running_notes`, `mode` is `dumb | smart`, and `auto` requires
  Smart. There is no multi-target `both` command.
- The capture composer flushes its draft before dispatch and clears only after
  persistence succeeds.
- Running Note contracts already anticipate stable IDs, versions, and
  tombstones.

### Gaps

- The production-shaped React surface defaults to `InMemoryJournalProvider`.
- `LegacyFlaskViewAdapter` is read-only.
- That adapter projects Today/timeline data only; it is not a hidden legacy
  capture/Log/Running Notes writer that a production provider can simply reuse.
- Backend Journal state remains Markdown-canonical in `work_buddy/journal.py`
  and `work_buddy/journal_backlog/*`.
- Daily Log mutation is read-modify-write; Running Notes are copied or
  destructively rewritten/routed.
- Current Log replay protection compares formatted line text, so it cannot
  distinguish two legitimate identical occurrences and cannot resolve every
  crash between a Markdown write and an SQLite acknowledgement.
- There is no production Quick Capture persistence API that atomically retains
  exact input and durable processing intent.

The existing frontend contract is therefore a strong specification seam, not
yet a production source capture path.

## Co-work documents

### What exists

- Canonical document persistence binds structured/Yjs state to a Markdown
  projection, generation, and hashes.
- Import/paste provenance attestations distinguish authorship, review, source,
  basis, attester, and supersession. That separation is a strong pattern.
- Relative positions and exact quote selectors already support durable target
  capture and reanchoring.

### Gaps

- `work_buddy/truth/ydoc_store.py` stores Yjs updates as opaque bytes.
- Ordinary update append records structured-head causality but no durable
  content author, source references, accepted proposal, or change kind.
- A compaction/materialization actor is not the author of every changed
  character.
- There is no domain-document binding for Journal or task entities.
- There is no durable document change record tying base/result heads, source
  item, exact affected content, selector, applier, reviewer, and assurance.
- Python intentionally has no Yjs runtime. Browser TypeScript builds initial
  structured documents and materialized Markdown projections; the server
  validates and stores the pair but cannot itself construct a headless
  document or source insertion.

That last point is a hard dependency. A future Python Journal worker cannot
honestly implement `insert_source_into_cowork_document` without either a
shared headless structured-document runtime or a deliberately trusted client
materialization protocol.

The existing editor extension bundle also includes browser/UI affordances. A
plain-production-Node, DOM-free document kernel does not already exist merely
because current tests run TypeScript under a browser-like environment.

## Tasks

### What exists

- The task master list, status, scheduling, and Obsidian Tasks compatibility
  are established and should remain in the Tasks domain.
- Task notes already have stable UUID-oriented naming in normal use.

### Gaps

- Task note bodies are raw `tasks/notes/<uuid>.md` files.
- Paths and Markdown reads are coupled across task mutations, IR sources,
  context collection, and conversation observability.
- A direct cutover would silently break readers. Migration requires a content
  adapter, shadow import/materialization, comparison, and measured cutover.

## Events, retry, and artifacts

### Events

The event spine is a useful durable at-least-once delivery mechanism with
deduplication, offsets, and dead-letter handling. Its store is separate from
source/domain transactions and its retention is operational. It cannot be the
atomic ingress outbox or canonical source authority. SSE remains an
invalidation signal, not durable state.

### Retry queue

The sidecar retry queue is appropriate for retrying operations and agent/model
execution. It is not the authoritative record that exact human input was
captured or that a domain effect is due.

### Artifact registry

`work_buddy/artifacts` is a lifecycle/cleanup composition registry. It is not a
source identity, provenance, resolver, authorization, or redaction subsystem.

### Dashboard identity boundary

The current dashboard is not an authenticated human boundary. Co-work request
identity can come from caller-controlled local headers or the generic
`dashboard-user` fallback, and configuration may expose the unauthenticated
service on `0.0.0.0`/Tailscale rather than loopback only.
`user_initiated()` is a consent convention, not proof that a human clicked or
composed text. PR 1 therefore must add the authenticated loopback principal/
session/gesture boundary described in the target architecture. Unsupported
remote capture remains conservatively labeled and cannot satisfy human-only
mutation policy.

### Backups

The current vital-backup design tars data and can upload it unencrypted to
private GitHub Releases; annual retention is not bounded. A pre-redaction
restore could therefore resurrect source bytes unless redaction state is fenced
outside the restored snapshot or content is cryptographically erasable. Sources
cannot be added to backup by checklist alone; encryption, retention, user
disclosure, blob consistency, restore ordering, and redaction replay are
architecture prerequisites.

### Existing file source observation helper

`work_buddy/cowork/source_observation.py` is a bounded no-follow mutable-file
reader for a particular Co-work workflow. The new shared `SourceObservation`
record must not silently reuse that name/path as though the helper were already
the system described here.

## Reuse versus replace

| Existing component | Decision |
|---|---|
| Truth normalized ledger and lifecycle | Preserve and extend through a migration/export revision. |
| AI Truth durable staging/runtime | Preserve; correct canonical multi-actor attribution and source integration. |
| Co-work action snapshots | Preserve for exact document targets; do not generalize them into the universal source record because they are document-bound. |
| Conversation store | First resolvable first-party conversation provider; no destructive row rewrite. |
| Transcript browsing providers | Preserve for browsing; add a distinct exact-item resolver contract later. |
| Journal React capture contract | Preserve and back with a real provider/API. |
| Journal/Tasks Markdown | Compatibility source and projection during migration, not indefinite dual authority. |
| Co-work provenance attestations | Preserve; distinguish attestations from server-verified change records. |
| Events/SSE | Use for notification/invalidation after authoritative commits. |
| Generic retry queue | Use for execution where suitable, not source capture authority. |

## Historical compatibility classifications

- Existing `wb-session` locators: `legacy_unresolved` until explicitly resolved.
- Existing role-less support links: `legacy_unspecified`.
- Existing AI Truth records with collapsed authorship: retain history; append a
  provenance correction only when preserved runtime data proves it.
- Existing conversation `user` messages: provider-reported role unless a
  trusted ingress record proves stronger identity.
- Existing pasted/imported document spans: retain their current conservative
  provenance; never infer user authorship from the import gesture alone.
