# Migration, validation, and risks

## 1. Migration principles

1. Never infer stronger provenance during migration.
2. Never make a legacy projection and a new store peer write authorities.
3. Add adapters and shadow reads before switching writers.
4. Make every migration restartable, idempotent, and auditable without raw text
   in logs.
5. Preserve stable user-facing identities and links wherever possible.
6. Keep reversible compatibility projections until parity is measured.
7. Treat application-level readable-content deletion and restore/export
   consistency as part of the migration—not as cleanup work.

## 2. Data evolution

### Sources schema v1

Sources starts as a new machine-level database and content-addressed blob store.
It must be registered with:

- an explicit decision about whether source bytes enter remote backup at all;
- encrypted backup, retention, disclosure/consent, blob-aware atomic restore,
  and an external redaction manifest or equivalent key-erasure fence;
- redaction/recovery-export invalidation;
- health/repair and integrity checks;
- storage usage and orphan-blob reconciliation;
- explicit retention policy; and
- machine/vault scope discovery.

Do not classify durable source items as ordinary TTL artifacts.

The Sources store has a persistent `authority_id`. Export/import preserves the
authority-qualified item identity when payloads agree, quarantines collisions,
records any explicit remap, maps actor issuers conservatively, never activates
imported access grants automatically, and imports pending outbox effects
suspended/inert.

Idempotency is scoped by tenant/authority, trusted issuer, submitting principal,
and client mutation ID (or uses an issuer-guaranteed globally unique key).
Canonical request hashes include exact representation bytes/digest, input mode,
target/effect envelope, actor/scope, retention class, and durable authorization
fingerprint. Client-stated occurrence time, provider-observed time, server-
received time, and committed time remain separate.

### Truth migration/export revision

The expected next Truth revision should be deterministic and preserve all v8
records. At minimum:

- old `wb-session` locators remain unchanged and become
  `legacy_unresolved` in the new read model;
- old role-less support edges project as `legacy_unspecified`;
- existing profiles preserve zero-required-support behavior;
- AI Truth claims created under collapsed provenance remain historical;
- append a provenance correction only where preserved runtime/output/decision
  records prove each actor dimension;
- source-backed evidence adds a portable source-resolution record and exact
  snapshot/export behavior; and
- export/import works with the global Sources database unavailable.

The actual schema/export number is fixed when the migration lands; v9 is the
expected next version, not a reason to skip compatibility review if `main`
advances first.

### Conversations and transcripts

Do not rewrite existing conversation rows to claim a stronger issuer or human
identity. A new source provider can resolve the row and report the precise
legacy assurance it has.

Do not backfill harness source items from normalized transcript turns. Only a
raw provider record with stable identity and exact content can become a
source-authoritative item. Unresolvable history remains browseable transcript
data.

Historical parsing does not solve current-turn loss. A future Claude Code/Codex
host bridge must capture the exact raw prompt before host normalization or TTL
deletion, preserve provider/native conversation/native item identity separately
from the extracting agent session, and inject an opaque prompt-scoped
`SourceRef` rather than a “latest message” pointer. Crash tests cover capture
before context injection, duplicate hook delivery, later transcript deletion,
and unsupported-host fallback to conservative file import.

### Hindsight/memory

Existing memory entries are not rewritten into source-backed claims or labeled
verbatim. The new projection path creates idempotent records keyed to Truth
claim identity/generation and explicit projection policy. Only eligible current
confirmed claims project by default; lifecycle/source-redaction invalidation is
replayable. Legacy memory remains legacy unless an exact Truth/source chain is
independently established. Current LLM-backed Hindsight retain is routed through
Agent Execution's disclosure manifest; the system does not call projection
deterministic until a non-LLM reference adapter actually exists and passes the
lifecycle reconciliation suite.

### Journal

Migration uses four states per logical day/note:

1. `legacy_authoritative`;
2. `shadow_imported`;
3. `new_authoritative_with_projection`; and
4. `retired_or_tombstoned`.

Shadow imports record content digest, source file observation, importer actor,
unknown/attested authorship, and comparison result. Cutover requires a clean
comparison window and a rehearsed rollback.

### Task notes

Preserve `note_uuid` and existing `[[uuid|📓]]` compatibility. Add a binding
instead of changing every link. All readers must move behind the adapter before
writer cutover. Missing/duplicate/path-divergent notes become explicit migration
exceptions; do not silently choose one.

### Co-work

Existing document versions and provenance attestations remain valid historical
records. New document change records begin at deployment. Do not synthesize
per-change authorship for old opaque Yjs updates.

## 3. Rollout and feature gates

Use explicit, independently reversible gates:

| Gate | Default during rollout | Rollback behavior |
|---|---|---|
| Sources exact capture | Explicit dev/opt-in, then authoritative for new Quick Captures; never shadow-copy raw content silently | Keep old draft; no downstream effect until source commit. |
| Journal HTTP provider | Opt-in/dev, then production default | Return to legacy read adapter while source captures remain durable. |
| Source-backed Truth composite | Opt-in by profile/surface | Genuine human manual authoring may remain; the old collapsed-provenance AI commit is upgraded or disabled before new-schema writes. |
| Truth → Hindsight projection | Off until the complete conversation-source/Truth fixture passes | Disable projection worker; canonical Truth/source records remain unchanged and projections reconcile on re-enable. |
| Headless structured runtime | Dev/test, then controlled source insert | Interactive editing remains available; queued headless effects stay pending. |
| Journal Co-work authority | Per-Running-Note/per-logical-day authority epoch, enabled by cohort | Restore legacy authority only inside bounded rollback window and regenerate projection. |
| Task-note Co-work authority | Per-task-note-UUID authority epoch, enabled by cohort | Adapter reads legacy Markdown until cutover is explicitly resumed. |

Do not use a feature flag to create two concurrent authorities. A flag selects
one writer and records the authority epoch.

## 4. Validation matrix

### Exactness

- leading/trailing spaces and blank lines;
- CRLF/LF and normalization policy;
- emoji, combining marks, surrogate pairs, and grapheme boundaries;
- multipart messages and ordered blocks;
- repeated identical substrings and duplicate messages;
- binary/media source items;
- oversize/partial content with explicit truncation; and
- selector repair that never widens silently.

### Identity and authorization

- same item ID in two Sources authorities does not collide;
- provider-native IDs collide safely inside distinct `OriginRef` namespaces;
- same text in two messages remains distinct;
- origin revision change does not rewrite source-item identity;
- `SourceRef` possession without a grant cannot dereference content;
- purpose-bound Truth access cannot be reused for export;
- UI/host ingress establishes only the exact payload, local submitting
  principal/gesture, input mode, and assurance its threat boundary supports;
- loopback bootstrap/session/gesture replay, expiry, origin, CSRF, context
  mismatch, rotation, restart, and revocation;
- direct HTTP/request headers/Tailscale/MCP—or browser automation without
  control of an enrolled session—cannot mint a trusted principal or stronger
  author assertion than the implemented issuer boundary;
- non-loopback human-authority writes remain disabled without a verified remote
  principal provider;
- local kernel access and external model/provider egress are separately
  authorized;
- legacy role=`user` remains conservatively labeled; and
- collaboration-ready issuer/subject scope prevents cross-user confusion.

### Atomicity and recovery

- crash before/after blob staging, row commit, outbox commit, delivery, domain
  commit, acknowledgement, and cleanup;
- lease owner death and retry;
- request timeout with server completion unknown;
- repeated user click and cross-tab race;
- same idempotency key with a different payload;
- consumer success with usage/effect acknowledgement failure;
- source redaction after resolution and before domain commit;
- pending usage reservation/reconciliation and incomplete redaction status;
- imported pending outbox rows remain inert;
- partial multi-effect completion; and
- repair after one domain database is temporarily unavailable.

### Truth

- evidence/span/claim/claim-evidence relation/derivation all commit or none do;
- exact source quote copied by kernel, not model;
- claim reuse with a new source does not change claim identity;
- AI semantic producer, candidate decision actor, lifecycle decision actor,
  and substantive reviewer (when one exists) remain distinct in rows, export,
  and UI;
- unchanged AI candidate; human proposition/kind/structured/scope/time revision;
  evidence-only selection edit; expression-role correction; and connection to
  an existing claim preserve outcome-correct producer/matcher/reviser/assessor
  provenance;
- positive usable-support counting, nonpositive claim–evidence effects, and
  claim-kind policy;
- document/paragraph/global applicability and open/closed valid-time fixtures,
  including temporary preference expiry and explicit supersession;
- source change/unavailability/redaction attention behavior;
- legacy migration/read projections; and
- standalone export/import without the source store;
- exact Work Buddy message → candidate → separate confirmation → current
  Hindsight projection, plus challenge/reject/supersede/expire/redact removal;
- Hindsight summary is always derivative and never returned as verbatim source;

### Co-work

- browser/headless runtime parity;
- source exact copy versus derived rewrite;
- compaction and relative-position survival;
- concurrent human edit versus prepared source change;
- ambiguous materialization recovery;
- domain-binding repair and retirement;
- defensible assurance labels; and
- plain production Node runtime without jsdom, managed health/reset, and
  browser/runtime version skew;
- no regression to editor/review navigation, persistence, or scroll behavior.

### Journal and Tasks

- logical day/timezone and daylight-saving boundaries;
- stable Running Note identity/carry-forward/tombstone;
- Markdown materialization parity and rebuild;
- external Obsidian edits captured/reviewed and never overwritten, including
  unknown daily sections;
- every durable Co-work head for an authoritative entity—including an ordinary
  direct editor update—advances its idempotent Markdown projection cursor, with
  missed wake-up, ambiguous write, and divergence recovery;
- authority epochs are independently testable per Running Note, logical-day
  Log, and task-note UUID; cohorts are rollout controls only;
- task note UUID/link/read/index/context/observability compatibility;
- task-note create/delete/retire saga recovery;
- shadow mismatch handling; and
- rollback at each authority epoch.

### Privacy and redaction

- source blob, database text, search index, Truth evidence/span, Co-work
  projection/change display, Journal/task Markdown, exports, caches, and
  temporary staging;
- pending redaction recovery never makes an unrelated store appear empty or
  unreachable;
- unexpected redaction/export failure remains fail closed without implying a
  canonical commit did not happen;
- content-free logs and DLQ records; and
- model-provider disclosures, semantic derivatives, issued exports, and
  unmanaged-copy warnings;
- restoring a pre-redaction snapshot is fenced by an external current redaction
  manifest or equivalent erasure boundary before readable state is exposed; and
- WAL/free-page/filesystem-recovery limitations are reflected in the declared
  storage threat model rather than called guaranteed physical erasure.

## 5. Observability

Expose typed operational metrics and repair views without raw content:

- source commit/outbox latency and state counts;
- oldest pending effect and retry distribution;
- orphan staged/final blobs and reference counts;
- resolver success/change/unavailable/mismatch by provider/version;
- redaction cascade completeness;
- Journal/task shadow parity and mismatches;
- headless runtime version/skew/failure rate;
- Truth source-composite transaction outcomes;
- legacy unresolved/unspecified counts; and
- domain binding orphan/repair counts.

Every user-facing failure must distinguish:

- exact source not yet persisted;
- source persisted but effect pending;
- effect failed and retry is safe;
- source cannot be resolved under current authorization;
- origin changed/unavailable but retained snapshot is intact;
- redaction in progress/incomplete; and
- domain unavailable without presenting an empty collection.

## 6. Principal risks and mitigations

### Risk: a cross-domain “god store”

**Failure mode:** Sources accumulates claims, tasks, document state, AI jobs, or
surface-specific workflow state.

**Mitigation:** package/API ownership tests and schema review. Sources stores
provenance and delivery facts only; domain effect payloads are typed references,
not copied domain tables.

### Risk: centralized privacy bypass

**Failure mode:** any component with a source URI can read all captured content.

**Mitigation:** purpose-scoped authorization, issuer/tenant/store scope,
content-versus-metadata permissions, audit trail, bounded exports, and denial by
default.

### Risk: false human attribution

**Failure mode:** `role=user`, paste gesture, model statement, or human approval
is treated as proof of authorship.

**Mitigation:** trusted ingress records exact submission/inputter under its
actual assurance; authorship is a separate append-only assertion with input
mode/basis. Add multi-actor records, conservative migration, and provenance
matrix tests.

### Risk: unauthenticated dashboard impersonation

**Failure mode:** caller-controlled local headers or unauthenticated direct
HTTP/Tailscale/browser automation is labeled an authenticated local-profile
gesture, or the UI overstates that gesture as proof of physical human presence.

**Mitigation:** PR 1 ships a persistent installation/local-profile identity,
single-use loopback bootstrap, revocable server session, origin/CSRF checks,
and exact context-bound gesture. Ignore caller-controlled identity fields.
Non-loopback human-authority writes fail closed until a verified remote
principal provider exists; weak capture is capped at
`local_surface_submission`. Test unauthenticated direct HTTP/Tailscale/browser
automation paths and state explicitly that automation controlling a valid
enrolled browser session falls inside the same-user compromise limit rather
than being cryptographically distinguishable.

### Risk: redaction cannot find copies

**Failure mode:** readable material survives in Truth, Co-work, Markdown,
exports, or logs.

**Mitigation:** pending usage reservation plus redaction epoch, domain-local
usage committed with the copy, central acknowledgement/reconciliation, domain
sweeps, no raw generic logs, application-level blob deletion, export issuance/
invalidation, derivative review, and end-to-end privacy tests.

### Risk: distributed transaction illusion

**Failure mode:** a UI claims all effects succeeded because exact input was
captured, or a retry duplicates effects after an ambiguous timeout.

**Mitigation:** source-owned transactional outbox, stable effect IDs,
destination receipts, authoritative reconciliation, and separate persisted/
processing status.

### Risk: two document runtimes drift

**Failure mode:** browser and headless worker produce different Yjs/Markdown
states.

**Mitigation:** one shared TypeScript schema/projection package, versioned
protocol, common golden fixtures, parity tests, and fail-closed version skew.

### Risk: trusted client attestation is mislabeled as proof

**Failure mode:** hashes in a client manifest are presented as server-verified
authorship despite opaque Yjs semantics.

**Mitigation:** assertion-specific assurance levels. The trusted TypeScript
document kernel owns structural verification; Python claims only authorization,
CAS, request/hash/source-byte, and persistence checks it independently performs.

### Risk: migration breaks Obsidian workflows

**Failure mode:** Journal files, task note links, indexers, or collectors stop
working after content authority moves.

**Mitigation:** adapters before cutover, shadow parity, compatibility
materialization with base hashes/generations, external-divergence capture/review,
reader/writer inventory, cohort rollout, and bounded rollback.

### Risk: backup/restore resurrects redacted sources

**Failure mode:** a long-retained or remote pre-redaction snapshot restores raw
source bytes and replays pending effects.

**Mitigation:** decide whether Sources is backed up remotely; require encryption,
bounded retention, an external redaction manifest or cryptographic-erasure
boundary, blob-aware restore validation, inert imported outbox work, and user
disclosure before enablement.

### Risk: model egress exceeds source authorization

**Failure mode:** a local Truth/Journal permission is treated as permission to
send raw source content to any account-backed or external model, which cannot be
recalled on redaction.

**Mitigation:** Agent Execution owns a run-scoped ordered multi-source
disclosure manifest. Before each content-bearing prompt/tool response, Sources
captures/resolves and reserves the exact item while the run records provider/
model/recipient, boundary, purpose, cost/consent basis, and expiry. Dynamic
search/fetch content receives a SourceRef first. Outcomes distinguish
`not_sent`, `sent`, and `possibly_sent`; ambiguous sends never automatically
replay. Model-produced search/connector arguments are captured as derived
source items and directional outbound entries bound to the input-manifest
digest; `possibly_sent` is committed before the provider call and promoted only
after acknowledgement. The UI discloses irreversible external disclosure before
it occurs.

### Risk: support vocabulary ossifies prematurely

**Failure mode:** ambiguous or overlapping relation labels become permanent
schema and UI burden.

**Mitigation:** validate the two-axis model against a fixture corpus and current
Truth workflows before marking v1 golden; preserve unknown/legacy without
guessing.

### Risk: retained sources grow without bound

**Failure mode:** machine-level storage becomes opaque and expensive.

**Mitigation:** explicit retention classes, deduplicated blobs, source/use
inventory, user-visible storage management later, and no retention change that
breaks evidence/audit contracts silently.

### Risk: scope expands into UX before authority is ready

**Failure mode:** attractive surfaces depend on ambiguous provenance and become
expensive to correct.

**Mitigation:** enforce the four PR exit gates. Add only the minimal review and
status affordances needed to test each slice.

## 7. Recommended defaults to ratify

The following are the plan's defaults and should not remain ambient ambiguity:

- Sources is machine-level with portable domain snapshots, not per-Truth-store
  duplication of the entire subsystem.
- Exact source item is retained before AI work.
- Trusted ingress is not MCP-callable and does not overclaim authorship.
- TypeScript/Node shares the canonical structured-document implementation;
  Python remains the authorization/persistence authority.
- Truth receives the second vertical slice, before mass Journal/task migration.
- Task state remains in Tasks; schedule state remains structured.
- Four meaningful stacked PRs are preferred over a monolith or 5+ small PRs.

After ratification, re-open one only if a prototype, threat model, packaging
constraint, or measured migration result demonstrates that the default cannot
meet its acceptance criteria.
