# End-to-end implementation plan

## Planning posture

The accepted delivery uses one substantial pull request with four
user-testable vertical-slice gates. Each slice establishes a durable foundation
for the next and can be exercised independently. The gates reflect genuine
authority and rollback boundaries, not separate merge units or code-layer
granularity.

Run `/wb-dev-pr` at the cumulative release boundary and keep architecture docs,
migrations, recovery notes, test evidence, and the PR description current as
each gate closes.

No frontend polish beyond the minimum interaction required to validate a
vertical slice belongs in these PRs. The larger Truth/Journal/Co-work UX pass
starts only after the foundation is proven.

## Implementation reconciliation

The accepted work is implemented as one cumulative branch. The `PR 1` through
`PR 4` labels retained below are historical architecture, test, and rollback
boundaries; they are not separate merge units and do not imply that a rollout
gate has been opened.

| Slice | Branch disposition | Deliberately closed boundary |
|---|---|---|
| 1 — Sources and Journal capture | Implemented: Sources authority, authenticated loopback ingress, production Journal provider, durable capture/outbox, and Log/Running Notes materialization | Journal smart processing is off by default |
| 2 — Truth and Hindsight | Implemented: source-backed Truth composition, multi-role provenance, Work Buddy conversation sources, Agent Execution disclosure, durable projection authorization/outbox/reconciliation | Truth-to-Hindsight projection is off until an exact durable authorization matches configuration |
| 3 — Co-work causality | Implemented: shared headless TypeScript document kernel, Python protocol/causality service, domain bindings, prepared/committed changes, projection, recovery, and minimal source inspection UI | Arbitrary per-character authorship and the larger frontend redesign remain out of scope |
| 4 — Journal and task-note migration | Implemented: compatibility adapters, shadow/parity tooling, per-entity authority epochs, projection/divergence handling, rollback, current Journal exit evidence, and operator seams | Journal and task-note cutover gates ship closed; arbitrary append into a Co-work-authoritative task note still fails closed |

Cross-cutting implementation also includes local backup policy, a central
read-only restore fence, high-consent reconstitution/quarantine paths,
source-usage redaction dispatch, and portable Sources/Truth recovery. See
[implementation-report.md](implementation-report.md) for the as-built summary
and the validation evidence available at this checkpoint.

The implementation is not equivalent to production migration. Opening a gate
requires current inventory/parity evidence, the applicable explicit consent or
authorization, a rollback window where required, and the final cumulative
release validation. Historical acceptance criteria below remain the source for
that validation; unchecked full-suite or process-kill exercises are not
implicitly complete merely because the code exists.

## Cross-cutting definition of done

Every slice must satisfy all applicable items:

- one canonical authority for each state transition;
- explicit exact-content and actor attribution semantics;
- idempotency conflict behavior, restart reconciliation, and crash injection;
- authorization enforced at the mutation/resolution boundary, not only hidden
  in UI;
- no raw input in logs, IDs, errors, events, or content-free operational/effect
  acknowledgements (`ResolvedSource` is a separate internal content-bearing
  object);
- migrations and exports round-trip deterministically;
- redaction and backup/recovery implications tested before enabling writes;
- old paths remain readable throughout compatibility windows;
- server/client schema versions fail closed on mismatch;
- authoritative GET/read models, with SSE/events used only to invalidate;
- focused unit/integration tests plus impacted legacy regressions;
- agent documentation/capability declarations rebuilt and validated whenever
  executable interfaces change; and
- PR description amended rather than overwritten at each checkpoint.

## Pre-implementation gate

Before opening PR 1, accept short ADRs for:

1. machine-level Sources authority identity, inline/blob ownership, export/
   import identity, encryption, backup retention, and restore/redaction fencing;
2. issuer-qualified ActorRef versus assertion-specific attribution assurance;
3. the actual loopback/Tailscale/dashboard/agent threat model and the required
   loopback principal/session/gesture boundary v1;
4. source retention, managed-copy usage handshake, semantic derivatives, and
   application-level readable-content deletion behavior;
5. canonical authority-qualified `SourceRef`, provider-native `OriginRef`,
   source representation identity, and native capture/reuse semantics;
6. domain portability and Truth evidence-snapshot policy;
7. claim–evidence schema, candidate-versus-lifecycle decisions, and
   compatibility default;
8. the trusted TypeScript headless document kernel, DOM-free package,
   deployment/runtime lifecycle, and assurance labels;
9. domain-owned binding authority, deterministic vault store placement,
   Journal/task document granularity, and externally edited Markdown policy;
   and
10. Agent Execution-owned, run-scoped multi-source disclosure manifests,
    dynamic-content source capture, send-boundary outcomes, and non-replay of
    ambiguous external disclosure.

The design in this package is the recommended answer to each. The ADR step
turns those decisions into code-facing contracts and catches any repository
constraint discovered during prototyping.

## PR 1 — Sources core and real Journal capture

### User-testable outcome

> I can enter exact text in Journal Quick Capture, route it to Log or Running
> Notes, leave/reload, and see that the input is durably saved even if all
> optional AI processing is delayed or fails.

This proves authenticated local-profile submission, exact persistence,
idempotency, outbox semantics, and a production frontend provider without first
depending on headless Co-work mutation. It does not overclaim that the surface
proves human composition or physical presence.

### A. Sources bounded context

Add `work_buddy/sources/` with clear package-level public APIs rather than
calling its SQLite store from domains.

Initial modules should cover:

- `models.py` — versioned `SourceRef`, `OriginRef`, `ActorRef`, representation,
  attribution, fidelity, observation, derivation, resolved-source, and
  source-resolution-record types;
- `store.py` — Sources schema v1, transactions, idempotency, outbox, usage
  reservations/acknowledgements, redaction epochs, and migrations;
- `blobs.py` — content-addressed staging/finalization/reference counting;
- `providers.py` — resolver registry and provider protocol;
- `ingress.py` — trusted first-party `commit_human_input` service;
- `resolve.py` — retained-source resolution, native-origin capture/reuse,
  purpose authorization, and re-observation;
- `disclosure.py` — source-side resolve/reserve/acknowledge primitives for
  external disclosure, without owning model-run state;
- `dispatch.py` — leased, restart-safe outbox delivery;
- `export.py` — content-aware authorized backup/export and import; and
- `errors.py` — typed content-free errors and HTTP mappings.

Extend `work_buddy/agent_execution/` with a run-owned source-disclosure layer:
a durable ordered multi-source manifest, pre-response gateway enforcement,
worker/tool-call/idempotency binding, directional `inbound_to_model` /
`outbound_to_provider` entries, and `not_sent | sent | possibly_sent` outcomes.
It calls Sources for capture/resolve/reserve/acknowledge but Agent Execution
remains the model-run owner. Search results/fetched pages and every other
dynamic content-bearing tool response must acquire a SourceRef before their
bytes can reach the worker. Model-produced search/connector arguments are
captured as derived source items and write-ahead `possibly_sent` manifest
entries before the external provider call, then promoted to `sent` only after
acknowledgement.

Do not expose `commit_human_input` through MCP. A dashboard route and future
host bridges receive a server-constructed `TrustedIngressContext`. PR 1 must
implement the loopback principal/session/gesture boundary from the architecture:

- persistent installation authority and enrolled local actor/profile IDs;
- short-lived single-use launch/bootstrap token for the loopback origin;
- revocable server-side session with opaque `HttpOnly`/`SameSite=Strict`
  cookie, origin/CSRF validation, expiry, rotation, and audit state;
- single-use gesture challenges bound to the authenticated session, visible
  action, exact subject/context digest, and expiry; and
- no trust in `X-WB-User-Ref`, request actor fields, same-origin alone, or
  `user_initiated()` as canonical identity.

Human-authority writes are loopback-only in v1. Non-loopback/Tailscale access
stays read-only for those writes until a separately authenticated remote
principal provider exists; any explicitly allowed weak capture is labeled
`local_surface_submission`. The stored input mode distinguishes `direct_entry`,
`paste`, `import`, `dictation`, `automation`, and `unknown`. The route
establishes exact payload and the authenticated submitting local profile/gesture;
human authorship still requires a separate basis/attestation.

### B. Journal capture records and API

Add a generic immutable ingress submission and versioned Journal command
envelope to the Sources transaction. Journal owns the resulting capture,
entry, routing, and processing-effect records; Sources does not become the
Journal workflow store. The durable submission/envelope/outbox are reference-
only (`SourceRef`, representation/selector, hashes, typed command, authorization
fingerprint) and never duplicate exact text.

API surface:

- `POST /api/journal/captures`
  - preserve the existing `wb.capture.submit` payload:
    `day_id`, `target_id = auto | log | running_notes`,
    `mode = dumb | smart`, `exact_text`, optional `stated_at`, intent ID, and
    client mutation ID;
  - validate/resolve `day_id` with authoritative logical-day/timezone policy;
  - reject `auto + dumb`; there is no `both` target;
  - record the exact input mode and any separate authorship attestation;
  - bind each smart/external effect to its durable narrow authorization,
    provider/model/egress class, disclosed content boundary, and expiry/pause
    policy;
  - no caller-controlled trusted author fields.
- `GET /api/journal/captures/{capture_id}` for authoritative reconciliation.
- `GET /api/journal/view` or the existing versioned Journal snapshot route for
  the complete current projection.
- event/SSE invalidation carrying only IDs/version, not input text.

The write response distinguishes source `persisted` from the Journal command
and any smart-processing effect state. `auto` classification is one retryable
effect that resolves to one destination; direct Log/Running Notes writes do not
need AI.

### C. Compatibility domain effects

Add `JournalContentAdapter` with authoritative read/snapshot,
`append_log`, and `append_running_note` operations. Inventory the existing
Journal writers; PR 1 routes capture materialization through the adapter, and
later migration phases move the remaining writers before authority cutover.

Each new capture creates a minimal structured Journal entry with a deterministic
entry/effect ID. Its Markdown projection includes a parser-recognizable hidden
ID/digest marker. The adapter uses prepared-write state plus that marker to
reconcile a crash after file write but before SQLite acknowledgement without
deduplicating two legitimate identical entries or appending one twice. Existing
unmarked Running Notes do not enter the actionable `JournalRunningNoteItem`
collection, whose contract requires a stable `itemId`; the hybrid provider
exposes them in a separate read-only legacy compatibility projection/notice
until migration assigns reviewed identities. Line position and placeholder IDs
are never used as durable identity.

The original source occurrence is canonical in Sources; the resulting Journal
composition remains under the existing Markdown authority until its explicit
per-entity authority cutover (pilot in PR 3, scaled in PR 4). This is not peer
dual-write authority: one is source history, one is current domain content.

### D. Production React provider

Implement a hybrid `HttpJournalProvider` conforming to the existing provider
contract: authoritative legacy Today/timeline data plus real capture/Log/
Running Notes records. Hide or explicitly mark unsupported demo-only widgets;
make it the production default only after its advertised capability model and
renderer conformance are truthful. Retain the in-memory provider for
Storybook/tests/demo fixtures.

Required UX states:

- saving exact input;
- saved, processing;
- saved, processing delayed/failed with retry;
- `auto` routing pending/failed/retried independently from exact persistence;
- mutation-ID conflict with draft retained;
- reload/reconciliation of all states; and
- no draft clearing before persistence acknowledgement.

### E. PR 1 tests

- `SourceRef` authority collision, import collision, URI hostility, and explicit
  remap-manifest behavior; `OriginRef` provider collision/path hostility.
- Exact whitespace/newline/Unicode/empty-boundary and byte-limit fixtures.
- Grapheme-boundary validation where a text selector promises it; code-point
  indexing alone is not sufficient.
- Loopback bootstrap redemption/replay/expiry/origin/CSRF/session-rotation and
  gesture binding/replay/context tests; direct HTTP, MCP, request headers, and
  actor body fields cannot mint a trusted principal or stronger author
  assertion.
- Non-loopback/Tailscale human-authority writes fail closed until a verified
  remote identity provider is configured; loopback remains usable.
- Same mutation key/same payload returns the same source/capture/effects.
- Same mutation key/different payload conflicts without mutation.
- Commit crash at every source/blob/outbox boundary.
- Outbox lease expiry, durable exact-effect authorization, retry, duplicate
  delivery, process restart, and authorization expiry/pause.
- Smart processing cannot return source bytes to a worker before Agent
  Execution records a run-manifest entry and Sources reserves the disclosure;
  every content-bearing tool response is covered, ambiguous sends become
  `possibly_sent` and are not automatically replayed, and already-injected host
  context is labeled preexisting rather than retroactively controlled.
- Model-produced search/connector arguments are covered as outbound derived
  disclosures bound to the input-manifest digest; `possibly_sent` is persisted
  before provider invocation and a crash never causes automatic external
  replay.
- Source saved while Journal compatibility consumer fails.
- Two identical entries at the same displayed time remain distinct; ambiguous
  Markdown commit reconciles by stable marker/effect identity.
- No raw content in logs/events/errors or content-free operational/effect
  acknowledgements.
- Journal provider conformance, reload, draft preservation, and SSE
  invalidation tests.
- Backup/export/import and application-level readable-content deletion of an
  unconsumed capture under the declared storage threat model.

### PR 1 exit gate

- Quick Capture works end to end with the sidecar restarted mid-processing.
- A database inspection proves exact source + outbox commit precedes effects.
- A failed effect is visible/retryable and never presented as lost input.
- No general agent operation can assign human authorship; surface-derived
  assurance never exceeds the implemented local threat boundary.
- The enrolled local actor and installation authority survive restart without
  becoming globally collision-prone; a valid bound loopback gesture can satisfy
  PR 2's human-decision policy.

## PR 2 — Source-backed Truth and correct provenance

### User-testable outcome

> AI can derive a proposed claim from an exact authorized source, while Truth
> shows that AI prepared/interpreted it and I decided whether to add/connect the
> candidate; that decision neither makes me its author nor silently confirms the
> proposition as true.

The same PR also proves the exported conversation's original path:

> I state a durable preference in an ordinary Work Buddy conversation, AI
> proposes it from the exact message, I review and separately confirm it, and
> only the eligible current Truth claim—not a purported verbatim memory
> summary—projects into Hindsight.

This validates the original motivation of the conversation against the shared
source foundation before broader domain migration.

**Hard entry gate:** PR 2 uses the persistent actor principal, authenticated
loopback session, and bound decision gesture delivered by PR 1. It cannot enable
human-labeled candidate/lifecycle decisions or the Hindsight path under the
weak `local_surface_submission` fallback. Remote surfaces remain unable to make
those decisions until their own verified principal provider exists.

Use the capability-disposition table in
[current-state-audit.md](current-state-audit.md) as the migration inventory:
public lifecycle/query primitives stay available, low-level acquisition remains
conservative, and the new composite calls internal store/kernel functions
rather than chaining public mutations.

### A. Truth schema/export evolution

Prepare the next deterministic Truth migration/export revision (expected v9)
for:

- portable evidence source-resolution records;
- new `evidence_relation` claim links whose `role_json` must validate as
  `claim-evidence/v1`, with positive effects counted as usable support;
- append-only provenance-attribution events keyed to claim/expression/evidence
  subjects, with stable ActorRef plus role (`semantic_producer`, `selector`,
  `candidate_preparer`, `matcher`, `semantic_reviser`, `evidence_selector`,
  `expression_relation_assessor`, `applier`, `execution_authorizer`,
  `substantive_reviewer`,
  `candidate_decision_actor`, or `lifecycle_decision_actor`), basis, assurance,
  run/source references, asserted time, and supersession;
- canonical candidate-decision events (`add`, `connect`, `dismiss`) ordered
  separately from claim lifecycle events;
- operation/idempotency result where not already durable; and
- per-profile/claim-kind support policy.

Do not rewrite historical authorship. Classify legacy data explicitly.
Existing `supports_span` links remain readable legacy support. New nonpositive
relations use `evidence_relation`; lifecycle queries count only validated
positive effects plus compatible legacy support. Unknown ad hoc `role_json`
projects as legacy opaque/unspecified rather than being guessed.

### B. Source resolver integration

Implement internal `wb-source` resolution in the Truth service. The agent-facing
operation supplies only `SourceRef`, representation-bound selector, expected
digest/revision, purpose, and candidate. The backend resolves before the Truth
write lock and passes `ResolvedSource` in-process (or an authenticated one-time
handle across a process boundary); it never accepts an agent-vouched receipt.

The first providers are explicit:

- capture the current AI Truth flow's frozen Co-work action snapshot/selection
  through `cowork-document` as a retained source item; and
- capture/reuse an exact stable message from Work Buddy's own conversation
  store through `work-buddy-conversation`. New messages retain ingress
  provenance; legacy `role=user` remains a conservative provider role rather
  than verified local-human identity.

This lets Analyze passage and an ordinary conversation preference exercise the
same foundation. One PR 1 Journal input fixture also proves the composite is
source-generic.

The Truth store copies the exact evidence snapshot and source-resolution record
needed for portable export. It reserves usage/redaction epoch before receiving
content, commits the usage ID with its evidence, then acknowledges/reconciles
centrally so redaction can reach the evidence, span quote, exports, and cached
projections.

### C. Atomic composite

Implement `truth_claim_propose_from_source` as one domain service and one
agent-facing capability over that service:

- validate `SourceRef`, representation, expected content/revision, local access,
  any provider/model egress authorization, claim kind, applicability scope, and
  valid time (`valid_from`/`valid_to`);
- resolve internally, reserve usage, and stage outside the store lock;
- kernel-resolve exact selector/quote;
- atomically create/reuse evidence, source-resolution record, span, claim,
  claim–evidence relation, derivation, local usage record, and idempotent result;
- finalize staged blobs or clean them on abort; and
- return a claim-with-support-receipts projection.

The agent supplies a selector/candidate proposition, never authoritative
human-authored quote content.

### D. Correct AI-assisted Truth commit

Refactor the current candidate-commit path so canonical records distinguish:

- AI run/model as candidate preparer, expression selector, and proposed
  matcher;
- exact operation/egress authorization actor and basis;
- substantive reviewer when review actually occurred;
- human semantic reviser when proposition, kind, structured meaning,
  applicability scope, or valid time changed;
- evidence-selection actor and exact selected set when attachments changed;
- expression-relationship assessor when `expression_role` changed;
- human candidate-decision actor;
- separate claim lifecycle-decision actor when present;
- Truth kernel as applier;
- source/document actors under their existing provenance; and
- exact evidence/document premises.

Apply an outcome-aware producer matrix: an unchanged new claim retains the AI
as semantic producer; an edit to proposition, kind, structured meaning, scope,
or valid time preserves AI candidate origin and records the human semantic
reviser/co-producer; an evidence-only edit changes only evidence-selection
provenance; an `expression_role` correction records a human expression-
relationship assessment; and connecting to an existing claim preserves that
claim's original producer while recording AI as matcher and the human as
connector. Identity-changing edits must be re-matched before commit.

The staged candidate and canonical claim retain Truth's existing structured
claim data, expression role, applicability scope, and `valid_from`/`valid_to`
fields. They are independently editable by the human and included in candidate-
decision and later lifecycle fingerprints. AI must not generalize “in this
manuscript,” “for this paragraph,” or a temporary preference into a global
enduring preference. Changed preferences use explicit supersession/valid-time
history.

Current AI Truth analysis remains an Agent Execution-owned run. Its initial
source-free worker prompt may launch first, but selected passage, existing Truth
context, search results, fetched pages, and every later content-bearing tool
response must pass the run-scoped disclosure boundary before bytes leave the
local kernel. Candidate commit-time validation is not a substitute for this
pre-response gate. Each candidate binds the complete ordered disclosure-
manifest digest.

Retain the strict human-only general mutation API. Introduce a narrow internal
multi-actor candidate-commit command rather than relaxing actor checks.

Candidate addition/connection and Truth lifecycle confirmation are different
canonical commands with different context fingerprints. If a future UI offers
one gesture for both, it must disclose and persist both decisions separately.
When the new schema starts accepting canonical AI-assisted writes, the current
collapsed-provenance candidate path is upgraded in place or disabled; it cannot
remain as an alternate feature-gated writer.

Current records remain historical. Where durable analysis runtime data proves
an attribution correction, append an explicit correction/derivation event; do
not mutate prior append-only facts.

### E. Claim–evidence relations and support policy

Validate evidential-effect and derivation axes independently. Add fixture-driven
policy tests before marking the exact enum golden. Preserve
`minimum_usable_supports=0` for existing profiles, then opt factual/research
profiles into stricter policy deliberately.

A direct human statement can become a separate human-assertion source, but a
confirmation click is first a lifecycle attestation. It supports an underlying
world claim only where claim-kind policy explicitly treats that speaker as an
authoritative source; it is never silently converted into external evidence.

### F. Conversation-to-Truth-to-Hindsight proof

Add the smallest complete user journey over Work Buddy's own stable
conversation/message IDs:

1. A new conversation message is retained or captured as one source item; the
   command never identifies it by turn index or text search.
2. The agent proposes a preference/claim using
   `truth_claim_propose_from_source`; the kernel copies the exact selected
   message excerpt and proposes explicit applicability scope and valid time.
3. The user can add/connect/dismiss the candidate. Addition remains separate
   from claim confirmation; proposition, claim kind, structured claim data,
   expression role, scope, and valid time are independently reviewable/
   correctable.
4. Only policy-eligible, current confirmed claims project into Hindsight by
   default. The projection stores the Truth claim reference, generation/status,
   applicability scope, valid time, and projection method—not a new
   authoritative quotation.
5. Challenge, rejection, supersession, expiry, source redaction, or claim
   invalidation updates/removes the projection idempotently.
6. Current Hindsight retain is treated as LLM-backed and uses Agent Execution's
   run-scoped disclosure manifest. A deterministic reference-only projection
   may become the default only after a genuinely non-LLM adapter is implemented
   and tested; it is not assumed from the desired architecture.
7. Hindsight summaries are labeled derivatives and can never be reused as
   verbatim user evidence without resolving the original source item.

This slice uses existing review surfaces and minimal status/link affordances; it
does not redesign Chat or Hindsight broadly.

Truth owns a transactional, content-minimized projection outbox written beside
every eligibility-relevant claim lifecycle change. An idempotent Hindsight
consumer records destination receipts/cursors, and a deterministic
reconciliation sweep compares current eligible Truth generations with live
projections. The ordinary event spine may reduce latency but is not the
delivery authority. Sources owns only the registered semantic-derivative usage
needed for source/redaction accounting.

### G. Minimal projection/UI changes

Use existing Truth review and claim detail surfaces to expose:

- exact source excerpt and source/provider identity;
- AI “Prepared by”; “Matched by” only for an existing-claim match; human
  “Revised by,” “Evidence selected by,” or “Expression classified by” only when
  applicable; candidate “Added/Connected/Skipped by”; substantive “Reviewed by”
  only when that review actually occurred; and claim-status
  “Confirmed/Challenged/Rejected by” as separate fields;
- claim–evidence relationship and whether it qualifies as usable support;
- structured claim fields and how the passage expresses the claim, with
  correction attribution;
- applicability scope and valid time without burying them in metadata;
- capture-time and current source state;
- legacy unresolved/unspecified labels; and
- source redaction/unavailability without erasing historical capture state.

Do not redesign the broader Truth IA in this PR.

### H. PR 2 tests

- Human-attributed source + AI selector + AI proposition + human candidate
  decision + separate claim lifecycle decision provenance
  matrix through API/export/import/UI.
- Agent cannot forge span author; resolver-backed kernel can use trusted
  attribution assertions without exposing the trust-bearing object to the
  agent.
- Selector duplicate/ambiguity/stale content/revision/identity mismatch.
- Transaction crash injection and staged blob cleanup at every composite step.
- Reused claim/evidence/span and concurrent idempotent requests.
- `claim-evidence/v1` role-schema rejection, positive support counting, ad hoc
  non-null legacy JSON classification, and `legacy_unspecified` preservation.
- Support policies for zero-support compatibility, strict factual claims,
  direct human assertion, contradiction, and unusable/redacted evidence.
- Central Sources store absent during Truth export import/read.
- Source unavailable/changed after successful capture preserves the historical
  exact evidence while creating attention state under policy.
- Current AI analysis regression proving canonical producer is the worker and
  decision actor is the human for an unchanged new claim.
- Outcome-aware provenance fixtures for material human candidate edits,
  including `structured`; evidence-only selection edits; expression-role
  correction; exact/equivalent connection to an existing claim; and an
  identity-changing edit that requires re-match. Export/import/UI must preserve
  the same distinctions.
- Weak local-surface identity cannot satisfy a human-only candidate/lifecycle
  decision or Hindsight eligibility; authenticated/bound principal can.
- Redaction cascade across source, Truth evidence/span/read models/exports.
- Source redaction between resolve and commit, pending-usage crash recovery,
  and model-provider egress authorization/irreversibility disclosure.
- Agent Execution-owned multi-source manifest over selected passage, existing
  Truth context, search results, and fetched pages; every content-bearing tool
  response is pre-reserved, dynamic content gets a SourceRef, candidate output
  binds the ordered manifest digest, and crash-at-send becomes non-replayed
  `possibly_sent`.
- Outbound search query/connector payload that quotes or semantically derives
  from protected input becomes a derived source and directional manifest entry;
  crash before/during/after provider invocation exercises write-ahead
  `possibly_sent`, acknowledgement to `sent`, and non-replay.
- Deterministic old-version-to-new-version migration and round trip.
- Ordinary Work Buddy message → exact source item → AI claim proposal → human
  candidate decision → separate confirmation → Hindsight projection.
- Two identical message texts with different message IDs stay distinct; legacy
  `role=user` is conservative; no turn-index/text-search resolution.
- Hindsight never labels its summary as verbatim, retains the Truth claim
  reference, and reacts to challenge/rejection/supersession/expiry/redaction.
- Document-local, paragraph-local, general, temporary, expired, and explicitly
  superseded preference fixtures; none may project with broader scope or longer
  valid time than the reviewed claim.
- Truth projection-outbox crash/replay, duplicate delivery, ambiguous
  Hindsight acknowledgement, missed event wake-up, and full reconciliation
  after each eligibility-changing lifecycle transition.

### PR 2 exit gate

- A real source-backed claim can be prepared, reviewed, exported, imported, and
  understood without the central Sources DB.
- The UI and rows never present AI-formulated content as solely human-created.
- The generic agent cannot manufacture human-authored evidence.
- The original conversation-source preference path works end to end without
  making Hindsight an authority.

## PR 3 — Co-work document causality and source-backed changes

### User-testable outcome

> I can capture a Running Note, have it materialize into its own domain-bound
> Co-work document without an open browser, and inspect where the exact text
> came from, who selected/applied/reviewed it, and exactly what the trusted
> document kernel versus Python persistence checks established.

### A. Shared structured-document package/runtime

First prove the current editor stack can run under plain production Node without
jsdom. Extract a DOM-free kernel extension bundle and the canonical
ProseMirror/Yjs schema, bootstrap, typed mutation, and Markdown projection logic
from browser-only code into a versioned TypeScript package used by both React
and a managed headless local worker. Menus, decorations, browser views, and
other editor affordances stay outside the kernel.

Define a narrow JSON/binary protocol with maximum sizes, schema/runtime
versions, request IDs, deadlines, cancellation/ambiguous completion semantics,
health checks, and content-free errors. Specify production packaging,
supervision, sidecar reset/rebuild behavior, resource isolation, and coordinated
runtime/browser version rollout. The worker accepts only typed operations; it
never receives arbitrary code.

The TypeScript runtime is part of the trusted document kernel and validates
structural selectors/projections. The Python side independently verifies only
what it can substantiate:

- exact expected base structured head/generation;
- result Yjs/update/projection hashes;
- runtime/protocol version and request/result binding;
- exact equality between source excerpt and declared copied text;
- source/destination selector identity, size/shape limits, and operation kind;
- authorization and review preconditions; and
- idempotency/recovery state.

### B. Bounded file-origin source adapter

Promote the useful safety properties of the existing bounded/no-follow Co-work
file observation into a real `work-buddy-file-import` Sources provider for
registered vault projections/imports. It records exact bytes/representation,
path identity separately in `OriginRef`, base digest/generation, symlink/root
checks, inputter versus unknown author, and size/encoding limits. External
Markdown divergence cannot be promised until this adapter can capture it as an
exact source under review; PR 3 gates the Running Note pilot on it.

### C. Domain-document bindings

Add binding storage and repair/query APIs without overloading
`document_class`. The domain record is authoritative for binding creation and
lifecycle; Co-work stores a reverse mirror for portability/query/repair.

Use one deterministic registered vault-owned domain-content Co-work store, never
an arbitrary active project or one sidecar per note. Granularity is one document
per stable Running Note, one managed Log document per logical day, and later one
document per task-note UUID. Schedule/planner state remains structured.

Because binding/change records live beside Co-work documents in the current
Truth sidecar, PR 3 includes its own deterministic schema/export revision and
round-trip tests rather than pretending PR 2's migration covers future tables.

### D. Prepared and committed document changes

Introduce durable change intent/state before crossing structured state and
domain metadata boundaries:

- prepared operation with source and base preconditions;
- materialized isolated result from the headless runtime;
- CAS commit of structured update + projection;
- committed `DocumentChangeRecord`;
- recovery/reconciliation after any ambiguous boundary; and
- source usage/effect acknowledgement.

Reuse the accepted-proposal sitting/materialization pattern rather than
allowing agents or background jobs to push raw Yjs updates.

Complete one pilot flow in this PR: a PR 1 `running_notes` capture gets a stable
Journal Running Note entity and authoritative domain binding, then the headless
kernel materializes its exact source-backed text into that document. A minimal
existing Journal action opens the bound Co-work document and its change/source
details. That pilot note enters a recorded per-note
`content_authority=co_work` epoch in PR 3; its Markdown becomes a base-hash/
generation-protected projection, and external divergence is captured for
import/review rather than overwritten. This avoids an editable Co-work document
and authoritative Markdown competing for the same pilot content. PR 4 scales
the already-proven authority transition. This is the slice's user initiation
and inspection path.

The pilot also implements the forward Co-work→Journal projection worker; the
authority flip is incomplete without it. For each authoritative binding it
compares the durable document head with the Journal projection cursor and uses
`(binding_id, content_authority_epoch, document_head)` as its idempotency key.
It records prepared state, renders through the shared kernel, performs a
managed-section marker/base-hash CAS, commits a receipt/cursor, and reconciles
ambiguous writes on restart. An event may wake it, but durable heads/cursors are
authoritative. An ordinary editor change must update Markdown; a divergent file
must be captured through `work-buddy-file-import` and pause projection for
review rather than be overwritten. The authority epoch is per Running Note
(and later per logical-day Log), while rollout cohorts are only feature gates.

### E. Change provenance projection

Project current authorship/source views from append-only change records. Do not
attempt per-keystroke semantic authorship for arbitrary raw human edits.

For each asserted property, expose its assurance:

- exact copied text can carry `document_kernel_verified`,
  `persistence_verified`, and/or `projection_verified` only for the specific
  assertions each mechanism checked;
- structural node mapping may be `trusted_surface_attested` if the server
  cannot reproduce it;
- imported authorship may be `user_attested` or `unknown`;
- model classification remains `inferred`.

Existing import/paste provenance attestations remain and can reference or be
superseded by stronger change records without being rewritten.

### F. PR 3 tests

- Runtime/browser parity for schema, bootstrap, each typed mutation, Markdown
  projection, and fixtures with complex Unicode/marks/blocks.
- Plain production Node execution without jsdom/browser-only imports, managed
  worker health/reset, and version-skew deployment tests.
- File-origin root/symlink/path/encoding/size defenses, exact divergence
  capture, unknown authorship, and review handoff.
- Runtime version mismatch, oversized payload, timeout, crash, and malformed
  output fail closed.
- Source exact copy versus formatting/rewrite/mixed-content attribution.
- Base-head race, concurrent edit, ambiguous recovery, and duplicate command.
- Change intent crash injection before/after Yjs append, compaction, projection,
  and receipt commit.
- Change record and stable target survival across compaction.
- Domain binding uniqueness, supersession, orphan repair, retirement, and
  export/import/restore under the domain-authoritative/reverse-mirror rule.
- Raw Yjs direct-edit path remains functional but makes only defensible
  provenance claims.
- Source redaction performs a controlled removal/tombstone for exact managed
  copies, and invalidates/routes semantic or mixed derivatives for policy review,
  without corrupting document state.
- Running Note capture → domain entity → binding → headless materialization →
  open/inspect vertical slice, including restart at every boundary.
- Ordinary direct editor update → committed head → idempotent Journal Markdown
  projection, including missed wake-up, duplicate delivery, crash around file
  write/receipt, and external-divergence pause/review.

### PR 3 exit gate

- Headless source-backed insert/replace works without an open browser.
- Trusted document kernel and browser produce identical canonical projections
  for the fixture corpus; Python labels only its own persistence checks.
- A restart at every materialization boundary reconciles to one committed
  document result or an explicit recoverable failure.
- The pilot note's Markdown projection follows every durable Co-work head and
  never overwrites an externally diverged managed section.
- Provenance UI never promotes an attestation/inference into kernel proof.

## PR 4 — Journal and task-note migration

### User-testable outcomes

1. Daily Log and Running Notes use structured Journal records and bound Co-work
   prose while preserving safe, editable Obsidian-compatible Markdown
   projection.
2. Task note bodies use bound Co-work documents while the task master list and
   UUID links remain compatible.

These are two separately gated migration tracks inside one large PR. Each
Running Note, logical-day Log, and task-note UUID has its own authority epoch;
cohorts only decide which entities may advance. Journal migration reaches its
exit gate before task-note writes can be enabled. If the combined review/
rollback surface becomes unmanageable, split only this boundary into a fifth
PR rather than hiding risk in one flag.

### A. Structured Journal migration

Introduce Journal Day, Capture, Log Entry, Running Note, and Processing Effect
records with deterministic migrations.

- Preserve logical-day/timezone behavior.
- Carry Running Notes by stable identity/reference, not destructive text copy.
- Bind prose to Co-work documents.
- Keep planner/schedule state structured.
- Shadow-read legacy Markdown and compare before cutover.
- Inventory and route every Journal writer/reader through
  `JournalContentAdapter` before cutover.
- After cutover, materialize Markdown as a compatibility projection with base
  hash, generation, managed-section identity, and section-level CAS.
- Treat Obsidian/external changes as supported inputs: never clobber divergence;
  capture the changed file/section as an exact source and require controlled
  import/review before the next projection.
- Preserve all unknown/unowned daily-note sections byte-for-byte.
- Keep a bounded rollback window; do not resume dual peer writes.

### B. Task-note adapter and migration

Route all task-note reads/writes through one adapter. Update mutation, IR,
context, observability, provenance, and dashboard consumers before changing
authority.

- inventory every note and hard-coded path reader;
- exact shadow import with conservative unknown provenance;
- bind stable `note_uuid` to Co-work document;
- compare normalized and byte-significant content according to explicit policy;
- switch writes to Co-work only after parity; and
- keep UUID Markdown projection/link compatibility;
- record projection base hash/generation and import external divergence without
  clobbering it; and
- implement creation/deletion/retirement/recovery sagas spanning task metadata,
  binding/document, projection, and existing retry state.

The task master list remains untouched.

### C. PR 4 tests

- Journal legacy parity over representative dates/timezones/Running Notes.
- Shadow comparison metrics and deliberate mismatch handling.
- Markdown projection rebuild, external divergence capture/import/review, and
  preservation of unowned sections.
- Task note reader/indexer/context/observability parity and UUID link survival.
- Journal epoch rollback before/after authority cutover, followed by a separate
  task-note epoch rollback rehearsal.
- Task create/delete/retire crash recovery at every saga boundary.
- End-to-end managed-copy/semantic-derivative redaction across Journal,
  task-note projection, Truth, Co-work, pending usages, and backup/restore
  fencing.

### PR 4 exit gate

- No production caller bypasses the Journal/task-note content adapters.
- Markdown is clearly authoritative-old before cutover and a managed-but-
  externally-editable projection after cutover; divergence is never overwritten.
- Per-entity Journal and task-note authority epochs can be observed, repaired,
  and rolled back independently of rollout cohorts.

## Follow-on work after the four foundation PRs

These remain in the architecture but do not share PR 4's migration rollback
boundary:

1. **Claude Code/Codex exact raw-item providers and current-turn bridges.** Add
   a source-authoritative API separate from normalized `TranscriptTurn`; never
   use turn index, text search, or mutable “latest message” state. A trusted host
   bridge must capture the exact prompt before normalization/TTL deletion,
   preserve provider + native conversation + native item identity separately
   from the extracting agent session, and inject only an opaque prompt-scoped
   `SourceRef`. Preserve intentionally distinct transcript/execution provider
   namespaces. Once acknowledged, capture survives later transcript deletion;
   unsupported hosts remain conservative file/import sources.
2. **Typed cross-domain effects.** Add task creation, schedule changes,
   considerations, and similar effects only with explicit destination contracts,
   exact durable authorization, provider/model egress policy, stable IDs, and
   outcome records. Prefer `triggered_by`/`based_on` unless stronger causality is
   established.
3. **Broader source rechecking and frontend/UX work.** Add provider policies and
   the richer cross-surface interaction after authority is stable.

A user-supplied ChatGPT export remains a file/import source unless a trusted
native provider establishes individual message identity. Parsing this design's
input file does not retroactively verify ChatGPT-native authorship.

## Cumulative release validation

After all four slices:

1. Run focused source, Truth, Co-work, Journal, Tasks, event, retry, backup, and
   redaction suites.
2. Run the full Python and React test suites and production React build.
3. Exercise one cross-domain scenario through process kills:
   - enter one exact Quick Capture;
   - persist it;
   - route to Journal;
   - derive and review a Truth claim;
   - insert an exact excerpt into a bound Co-work document;
   - run one explicitly authorized Journal smart-processing effect;
   - restart between every boundary; and
   - redact the source and verify all readable-copy workflows.
4. Export/import the relevant Truth and Sources data into a clean environment.
5. Verify no generic log/event/operation DB contains the raw captured text.
6. Rebuild agent docs and assert every public capability has a validated
   declaration and least-authority session policy.
7. Conduct an adversarial provenance review: every human/AI/source label in the
   UI must be traceable to a specific canonical record and assurance basis.

## Work explicitly deferred until the foundation lands

- broad Truth frontend/IA redesign;
- automatic whole-document/folder claim extraction;
- universal Co-think/Chat initiation of source-backed operations;
- external harness exact-source providers beyond Work Buddy's own conversation
  store;
- generalized task/schedule/consideration effects;
- historical harness backfill beyond exact resolvable raw items;
- per-character collaborative authorship for arbitrary edits;
- full hosted multi-user identity/federation;
- arbitrary third-party connectors;
- automatic claims for all captured Journal/task/conversation material; and
- source change rechecking policies beyond the first registered providers.
