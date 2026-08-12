# Target architecture

## 1. Boundary and ownership

Create `work_buddy/sources/` as a machine-level bounded context with its own
SQLite database and content-addressed blob directory.

It owns:

- immutable source-item occurrence identity;
- exact retained bytes/text and digest;
- authority-qualified retained-item resolution and provider-native origin
  coordinates;
- source author/inputter/capture attribution with explicit basis and assurance;
- append-only resolution, integrity, availability, derivation, and redaction
  observations;
- purpose-scoped access bindings;
- a downstream usage/dependency index for redaction and maintenance;
- an atomic source-owned outbox and idempotency records; and
- backup, export, import, and application-level readable-content deletion for
  its managed records.

It does not own:

- claims, support judgments, or fact status;
- collaborative document contents or document lifecycle;
- Journal routing, note lifecycle, or schedule structure;
- task state or task scheduling;
- agent run/candidate state; or
- a generic event stream for every domain.

## 2. Source data model

### `source_items`

One row represents one captured occurrence.

| Field group | Required semantics |
|---|---|
| Identity | Opaque `source_item_id`, persistent Sources `authority_id`, canonical structured `SourceRef`, schema version. |
| Content | Primary representation ID, media type, byte length, content SHA-256, inline content or content-addressed blob reference, character encoding where applicable. |
| Origin | Structured `origin_ref`, native revision, occurred time, committed time. No raw path/text in the public reference. |
| Role | Input kind such as first-party human input, conversation message, imported file, document selection, audio, transcript, fetched passage, or agent output. |
| Attribution | Current projection over append-only author/inputter/issuer assertions. Authors may be unknown, mixed, or multiple; basis and assurance belong to the assertion, not the ActorRef. |
| Context | Tenant/machine scope, originating surface, project/store namespace, sensitivity/retention class. |
| Idempotency | Issuer namespace, client mutation key, canonical payload hash. |
| Lifecycle | Active/tombstoned/redacted state and content-free pointer to the redaction event. |

Text equality does not merge rows. Blob equality may deduplicate storage with
reference counting.

For small first-party text, store exact UTF-8 bytes inline in the SQLite
transaction so the initial persisted acknowledgement has one durable boundary.
Larger/binary content uses write-ahead staging, file and directory flush,
atomic finalization, and restart reconciliation; “persisted” is returned only
once the blob is durably recoverable from the committed row.

### `source_representations`

A source item may have several non-interchangeable representations:

- exact raw bytes;
- decoded text under a recorded encoding;
- ordered multipart/native blocks;
- canonical text projection; or
- another demonstrably lossless derived representation under the rule below.

Every representation has an opaque ID, media/schema type, digest, byte/character
length, derivation relation, and producer. Selectors bind a representation ID
plus its state/digest. A selector over canonical text can never be replayed
against raw multipart content merely because both belong to one source item.

Lossless decoding, serialization, and deterministic versioned text projection
remain representations of one source item. A transformation that changes
semantics or authorship—speech recognition, translation, summarization,
rewriting, or substantive composition—creates a new source item with a
derivation edge. A selected excerpt normally remains a selector over its parent
representation; create an `extracted_from` item only when the excerpt needs an
independent lifecycle/attribution/retention contract. Formatting stays a
representation only when it is demonstrably lossless under the declared
canonicalization; otherwise it creates a derived item.

Selector schemas declare their coordinate unit. Raw-byte ranges use byte
offsets. Canonical text ranges use Unicode code-point offsets plus exact quote
and prefix/suffix repair context, with an optional requirement that endpoints
fall on grapheme-cluster boundaries. JavaScript UTF-16 indexes are converted at
the boundary and never persisted as though they were code points. Multipart
selectors first bind a stable part ID; Co-work document selectors additionally
retain their Yjs-relative endpoints and frozen projection state.

### `source_attributions`

Append-only attribution assertions identify role (`author`, `inputter`,
`speaker`, `issuer`, etc.), actor(s) or unknown/mixed state, basis, assurance,
optional representation/span selector, asserting actor/component, observed
time, and superseded assertion. This is the
foundation for provenance determination without rewriting immutable source
content.

### `source_observations`

Append-only observations record what a registered resolver found at a point in
time:

- `captured` / `resolved`;
- `snapshot_integrity_ok` / `snapshot_integrity_failed`;
- `origin_unchanged` / `origin_changed`;
- `origin_unavailable`;
- `identity_mismatch`;
- `redacted`; or
- `resolver_failed`.

Each carries resolver ID/version, observed time, native revision/hash, content
hash, actor/role/fidelity observations, status, and bounded content-free error
classification. Observation failure never destroys the retained snapshot.

Content access audits are a separate operational record class. Reading a
retained snapshot does not fabricate a new origin-health observation; a source
observation is appended only when a resolver/integrity operation actually
examines the origin or retained representation.

### `source_derivations`

Typed edges connect derived source items to their inputs:

- `quoted_from`;
- `transcribed_from`;
- `translated_from`;
- `summarized_from`;
- `revised_from`;
- `formatted_from`; or
- `extracted_from`.

Each edge identifies producer/activity, method/tool/model where applicable,
fidelity, and exact selectors or transformation metadata. A voice transcript,
for example, is attributed to the transcription activity and points to the
audio source; it is not relabeled as exact human-authored text.

### `source_idempotency`

The uniqueness key is scoped by Sources authority/tenant, trusted issuer,
submitting principal, and `client_mutation_id`, unless the issuer contract
guarantees a globally unique key. It stores a canonical request hash and
resulting source/submission IDs. The hash binds the exact representation,
input mode, target/effect envelope, actor/scope, retention class, and durable
authorization fingerprint.

- Same key + same hash returns the original durable result.
- Same key + different hash returns a conflict without mutation.
- A retry after process death reconciles the original result.

Client-stated occurrence time, provider-observed time, server-received time, and
committed time remain separate fields with explicit bases.

### `source_access_bindings`

Source resolution is authorized independently of reference possession. A
binding covers:

- principal/actor or trusted service;
- machine/project/store/domain scope;
- allowed purpose (`journal_effect`, `truth_evidence`, `cowork_insert`, export,
  review, recheck, redaction);
- content versus metadata access;
- local-kernel use versus external model/provider disclosure, including bound
  provider/model/egress class and content boundary;
- expiry/revocation; and
- optional user gesture/authorization receipt.

### `source_usage_intents` and `source_usages`

Every consumer registers a durable use:

| Field | Meaning |
|---|---|
| consumer domain | Truth, Co-work, Journal, Tasks, scheduling, export, etc. |
| consumer identity | Store/document/claim/evidence/change/effect identifier. |
| use kind | Evidence snapshot, exact insertion, derivation premise, display copy, projection, or issued export. |
| disclosure kind | Exact readable copy, bounded excerpt, semantic derivative, metadata-only dependency, external model/provider disclosure, or issued offline copy. |
| redaction policy | Tombstone, scrub, invalidate, rebuild, or policy-driven human review. |
| last acknowledged state | Idempotent redaction/maintenance progress. |

Cross-database use follows a reserve/commit/acknowledge protocol:

1. Sources reserves a deterministic pending usage and binds the current
   redaction/access epoch before giving a consumer content or an authenticated
   resolution handle.
2. The domain transaction writes its source reference, usage ID, effect ID, and
   local provenance/readable-copy record atomically with the domain mutation.
3. The consumer idempotently acknowledges the usage in Sources.
4. A pre-commit recheck rejects a revoked/redacted epoch. If redaction races
   after that recheck, the pending usage keeps redaction incomplete and the
   eventual acknowledgement immediately joins the scrub/review cascade.
5. Reconciliation resolves pending usages after any crash. A pending or
   unresolved usage makes redaction completeness **unknown/incomplete**, never
   falsely complete.
6. Registered domain sweeps can rebuild/check the central index; the central
   mirror is not the sole evidence that no managed copy exists.

This is the redaction and change-maintenance dependency index, not a claim
graph. Offline exports/backups are issuance records with retention and warning
semantics; Work Buddy cannot promise to reach and erase an uncontrolled copy.
External model/provider disclosures likewise become immutable usage/disclosure
records bound to provider/model/run and content boundary; later redaction can
invalidate dependents but cannot recall a prompt already sent.

### `source_outbox` and domain effect receipts

The source transaction writes source item, ingress submission, idempotency
record, and requested downstream effects into the same database transaction.
Each outbox row has a stable `effect_id`, typed payload hash, target domain,
lease, attempts, terminal/retryable state, result identity, and a durable narrow
authorization basis for that exact effect. A replay does not inherit an expired
session/workflow grant. If no durable authorization covers execution, the
effect pauses for a new user decision.

Ingress submissions, command envelopes, outbox rows, and effect
acknowledgements are reference-only: `SourceRef`, representation ID, selector,
digests, authorization fingerprint, and typed parameters. They never repeat the
raw source payload. If a destination genuinely needs a durable readable copy,
that copy is committed in the destination with a reserved usage ID and enters
redaction accounting.

Consumers apply effects idempotently and record a domain-owned effect result.
Events/SSE may notify after commit, but the outbox remains the rebuildable
authority.

Outbox rows are not silently TTL-deleted while an effect remains pending or
while their idempotency result is part of the source contract.

### `source_redaction_events`

Redaction is append-only as an event and deletes/tombstones readable content at
the application level under the declared storage threat model:

1. authorize and bind the exact item/current redaction context;
2. mark the source item non-readable and remove its blob reference;
3. delete a now-unreferenced blob;
4. invalidate source exports/caches;
5. enqueue idempotent redaction effects for every managed exact copy, semantic
   derivative, and pending usage that policy says may disclose the source;
6. retain content-free IDs, hashes where policy permits, actor/authorization,
   times, and completion state; and
7. expose incomplete cascade work rather than claiming complete deletion.

Cryptographic erasure is optional future hardening, not part of the initial
claim. Backup retention, issued offline exports, and recovery-export handling
must be documented before production cutover. Redaction status distinguishes
managed-copy completion from copies Work Buddy cannot recall.

For an exact managed Co-work/Markdown copy, the redaction effect performs a
controlled document change that removes or tombstones the copied content while
preserving structural integrity and redaction history. A semantic or mixed
derivative may require policy-driven invalidate/review because automatic string
deletion would neither remove meaning reliably nor preserve the work safely.

Deleting a Journal entry, task note, claim, or document is not silently the same
operation as redacting its retained source. Product/API commands distinguish
domain deletion/retraction from source readable-content deletion, disclose the
retention consequence, and release/update usages idempotently. Policy-driven
garbage collection may remove an unreferenced source only under an explicit
retention rule; it never hides a retained invisible source by accident.

## 3. Storage and portability decision

The machine-level Sources store owns the canonical exact source snapshot. A
content-addressed blob may be physically shared by several logical records
within one installation.

Consumers still own portable provenance records:

- Truth retains its immutable evidence record, exact span/quote, source
  resolution observation, and content hash. Runtime may refer to the shared
  blob, but a Truth export embeds the authorized evidence snapshot or a
  redaction-safe tombstone so it remains importable without the global Sources
  database.
- Co-work retains the exact inserted-content digest, source reference, base and
  result document heads, changed range/selector, and change assurance. It does
  not need to duplicate a whole source file to prove one copied excerpt.
- Journal/Tasks retain their domain effect record and domain content after
  materialization.

This is intentional logical duplication of the minimum portable evidence,
with physical deduplication where possible. Every readable copy must register
a source usage so redaction can reach it.

## 4. Provider and resolver architecture

### Native-origin provider contract

A native-origin provider has a stable namespace and implements only the
operations it can substantiate:

- canonicalize/validate `OriginRef`;
- capture one exact native item as a retained source item;
- report native identity/revision and fidelity;
- re-observe current origin state;
- authorize content/metadata access; and
- return canonical native observations to the Sources kernel.

Normal retained-snapshot resolution uses `SourceRef` against the named Sources
authority and does not require the origin provider to remain installed.
Providers do not interpret Truth claims or modify documents.

The canonical native capture operation is
`source_capture_from_origin(provider_id, origin_ref, expected_revision,
expected_digest, purpose) -> SourceRef`. It resolves internally, retains exact
representations, and records the capture observation. For a provider that
guarantees stable native identity/revision, `(provider_id, canonical native
occurrence, part, revision)` is unique within one Sources authority: the same
digest reuses the item, while a different digest is an identity-mismatch
conflict. A later revision creates a new item linked by `revised_from`. For a
provider without stable occurrence identity, only the caller's explicit
idempotency key can reuse a capture; the system never merges by text.

### Planned ingress and origin adapters

1. `work-buddy-input` — PR 1 first-party Quick Capture ingress issuer (not a native
   origin re-resolver).
2. `cowork-document` — PR 2 exact frozen Co-work projection/selection with its
   structured head and selector.
3. `work-buddy-conversation` — PR 2 exact rows from Work Buddy's own conversation
   store, with legacy identity assurance kept conservative.
4. `work-buddy-file-import` — PR 3 bounded no-follow capture of registered-vault
   projection/import bytes, with source author unknown unless separately
   attested. It is required before external Markdown divergence can enter the
   controlled review path.

Later exact-record providers may cover Claude Code and Codex harness files.
They must parse stable raw native records rather than wrapping the normalized
`TranscriptTurn` browsing projection.

Those providers also need a **current-turn capture bridge**, not only a history
parser. Before a host normalizes, truncates, or TTL-deletes a prompt, its trusted
bridge captures the exact raw item under the native provider/conversation/item
identity and injects only an opaque prompt-scoped `SourceRef` into the agent
context. The extracting agent session is recorded separately from the native
conversation and item. There is no mutable “latest message” pointer. Once the
source commit is acknowledged, later transcript deletion cannot remove the
retained snapshot. A host that cannot provide this handshake stays a
conservative file/import source; an exported ChatGPT conversation, for example,
does not acquire native message identity merely because a parser can split it.

### Resolved source

A `ResolvedSource` is an immutable, canonically hashable, trust-bearing transient
object produced inside the backend. It includes:

- source reference and item ID;
- snapshot/content hash and media type;
- exact bounded content or authorized blob handle;
- origin reference and native revision;
- source author/inputter/issuer and their bases/assurances;
- fidelity classification;
- resolver ID/version;
- capture/current observation IDs;
- authorization/purpose context hash; and
- redaction/current availability state.

Consumers validate expected source/content/revision preconditions. It is never
round-tripped through an agent as proof. In-process callers receive it directly;
cross-process callers receive an opaque one-time handle or authenticated token
bound to principal, purpose, source, digest, redaction epoch, and expiry.
Domains persist a bounded `source-resolution record`, not this content-bearing
transient object.

## 5. Trusted ingress

`commit_human_input` is an internal service, not an MCP capability.

The server constructs a `TrustedIngressContext` from:

- approved local surface/host issuer and a server-minted, narrowly bound gesture
  token where the deployment can support one;
- issuer identity and version;
- submitting local principal and scope;
- gesture/session binding where required;
- originating surface/project/store;
- sensitivity/retention policy; and
- input mode (`direct_entry`, `paste`, `import`, `dictation`, `automation`, or
  `unknown`), optional separate authorship attestation, and permitted downstream
  effects.

The request supplies exact content and a client mutation ID; it cannot supply
an arbitrary trusted human author. The ingress service establishes the exact
payload, issuer, and submitting local principal/gesture. Even `direct_entry` is
a trusted-surface attestation, not cryptographic proof that no automation acted.
Paste/import/dictation default to unknown or derived authorship unless a
separately bound human attestation says more.

Exact persistence never waits on a provenance questionnaire. The item may begin
with unknown authorship, then a provenance-determination surface appends a bound
attribution assertion (for example: authored by this person, AI-produced and
reviewed by this person, mixed, or unknown). Browser-observed typing/paste mode
is useful basis metadata but remains a surface attestation under the stated
threat model.

Agent operations may name authorized `SourceRef` values. No general agent
operation can set human authorship or obtain a content-bearing resolution object
to vouch for back to a domain. Current `main` has no authenticated dashboard
identity boundary and accepts caller-influenced local identity, so PR 1 must add
the loopback issuer/session/gesture boundary below. Before enrollment—and on
unsupported remote surfaces—the system may record only the weaker
`local_surface_submission` assurance. Same-origin, `user_initiated()`, and
absence of an MCP capability are consent/routing conventions, not proof that a
human composed the payload. A process already granted browser/local-host
control may exercise the same surface.

Weak `local_surface_submission` is sufficient to retain exact input with a
conservative inputter assertion. It is **not** sufficient for a canonical actor
labeled human to make a candidate, review, confirmation, redaction, or other
human-authority decision. Those mutations remain disabled until the deployment
provides the defensible persistent actor principal and bound decision gesture
required by their policy; otherwise the actor stays a local-surface principal
and cannot satisfy a human-only rule.

### Loopback principal and gesture boundary v1

PR 1 delivers—not merely evaluates—the first boundary required by PR 2:

- the installation has a persistent random `issuer_authority_id`, and setup
  enrolls one persistent local profile/actor ID under that authority;
- the sidecar/desktop launch path mints a short-lived, single-use bootstrap
  token bound to that installation, profile, loopback origin, and audience;
  it is handed to browser code without trusting a request header as identity;
- redemption over loopback creates a revocable server-side session represented
  by an opaque `HttpOnly`, `SameSite=Strict` cookie, with origin/CSRF checks,
  expiry, rotation, and content-free audit state;
- a human-authority mutation requires a single-use server gesture challenge
  bound to that session, visible control/action, exact subject/context digest,
  and short expiry; and
- caller-controlled `X-WB-User-Ref`, body fields, same-origin status, and
  `user_initiated()` never determine the canonical actor.

V1 permits human-authority mutations only on the authenticated loopback
surface. A non-loopback/Tailscale deployment remains read-only for those
mutations until a separately verified remote identity provider maps its
principal into the same ActorRef model. Weak capture may remain available under
an explicit policy but carries only `local_surface_submission` assurance.

This boundary establishes “this enrolled local profile used this exact bound UI
gesture,” not physical presence, sole composition, or immunity from a
same-user-compromised browser, extension, or process. Those are explicit threat-
model limits, not hidden claims.

Browser draft persistence remains crash recovery. The durable boundary is the
server acknowledgement after source item + submission + outbox commit.

### Run-scoped source disclosure boundary

Agent Execution remains the owner of model-run lifecycle, authorization,
provider/model selection, budgets, worker identity, and recovery. Sources does
not start or own runs. It provides authorized resolution/capture plus
redaction-epoch usage reservation and acknowledgement.

Each run has a durable, content-minimized **multi-source disclosure manifest**
owned beside Agent Execution state. Entries are directional:
`inbound_to_model` for prompts/tool results and `outbound_to_provider` for
model-produced tool arguments that leave Work Buddy. Before any content-bearing
initial prompt or tool response returns source bytes to the worker:

1. the gateway validates the run, purpose, provider/model/recipient, cost and
   egress policy, tool call, representation/selector, and exact content bound;
2. a dynamic input—such as a search result or fetched page—is first captured
   through its provider as an exact bounded source item; snippets alone never
   masquerade as fetched evidence;
3. Sources resolves the retained source and reserves an external-disclosure
   usage under its current redaction epoch;
4. Agent Execution commits a manifest entry bound to run, worker session, tool
   call/idempotency key, `SourceRef`, representation/selector, digest, byte
   bound, recipient/provider/model, authorization, and reservation;
5. immediately before releasing those exact bounded bytes, Agent Execution
   durably advances the entry to `possibly_sent`; after successful handoff it
   promotes the entry to `sent` and acknowledges the Sources usage; and
6. candidate output binds the complete ordered disclosure-manifest digest, not
   merely the first selected passage.

Every entry has one of `not_sent`, `sent`, or `possibly_sent`. A crash after the
irreversible send boundary but before acknowledgement becomes `possibly_sent`;
it is never automatically replayed as though no disclosure occurred. Reconcile
the content-free manifest/usage state without resending bytes. `not_sent` is
used only before write-ahead send intent or when the transport can prove no
bytes left Work Buddy; the conservative write-ahead may therefore overcount a
crash-before-call rather than undercount a disclosure. The same protocol
covers the selected passage, bounded existing Truth context, later search/fetch
receipts, and any other content-bearing capability response. An initial launch
prompt that contains no source content can start the worker before these
entries; the first source-bearing tool response cannot bypass the boundary.

Outbound tool arguments use the same write-ahead rule. Before a web-search,
fetch, connector, or other provider call can receive a model-produced query or
payload, the exact bounded argument is retained as an agent-produced derived
source item (with derivation links to the run's input-manifest digest where
supported), Sources reserves the disclosure, and Agent Execution writes an
`outbound_to_provider` entry bound to recipient/provider, argument digest and
boundary, authorization, tool call/idempotency, and derivation basis. It is set
to `possibly_sent` **before** the irreversible external call and promoted to
`sent` only after acknowledgement. Thus a search query that quotes or
semantically reveals protected source material is accounted for even though it
originated as model output.

If the host already injected the content into a general agent outside this
boundary, Work Buddy records only `host_context_preexisting` provenance and
does not retroactively claim it controlled or authorized that disclosure. The
later kernel still re-resolves exact source text before a canonical mutation.

The existing Hindsight retain path is treated as LLM-backed unless and until a
genuinely non-LLM deterministic projection adapter is implemented and proven.
Any LLM-backed retain/summarization uses Agent Execution plus this disclosure
manifest before it sees claim/source-derived content. A future deterministic
reference projection may become the default only after its non-LLM path and
lifecycle reconciliation are tested.

## 6. Truth integration

### Composite operation

Add a canonical service/capability named
`truth_claim_propose_from_source`.

```mermaid
sequenceDiagram
    participant A as "Agent or trusted caller"
    participant T as "Truth service/kernel"
    participant S as "Internal Sources resolver"
    participant H as "Human decision-maker"
    A->>T: SourceRef + selector + expected constraints + claim candidate
    T->>S: Resolve, reserve usage, and bind redaction epoch
    S-->>T: ResolvedSource + reservation
    T->>T: Stage portable bytes outside the Truth write lock
    T->>T: One atomic Truth write including domain-local usage
    T->>S: Acknowledge usage after Truth commit
    S-->>T: Acknowledged or pending reconciliation
    T-->>H: Proposed claim with support receipt
    H->>T: Bound candidate decision; lifecycle decision remains separate
```

The resolver and Sources reservation run **before** the Truth write lock. Any
new portable evidence blob is also staged before that lock. The reservation is
bound to the exact source representation, purpose, destination, and redaction
epoch.

One Truth transaction—and only that local database transaction—then:

1. validates the resolved-source/reservation preconditions and expected
   content/revision;
2. creates or reuses the evidence record;
3. copies the source-resolution record required for portable Truth export;
4. resolves the representation-bound selector and copies the exact span from
   authoritative content;
5. assigns span authorship from trusted attribution assertions, never the agent
   request;
6. creates/reuses the claim with the actual semantic producer;
7. validates and attaches the claim–evidence relation;
8. records derivation/premises and the distinct human/agent actor dimensions;
9. records the domain-local usage row; and
10. records the idempotent operation result.

After the Truth commit, the service finalizes/cleans staged blobs as required
and acknowledges the usage in Sources. If acknowledgement fails, the Sources
reservation and Truth-local usage row are sufficient for an idempotent
reconciler to finish it. If the Truth transaction aborts, cleanup/reconciliation
releases the unused reservation. No Sources call occurs while the Truth write
lock is held, and the design makes no distributed-transaction claim.

The generic `truth_span_mark` anti-forgery check remains strict. Only this
resolver-backed kernel path may establish human span authorship when invoked by
an agent selecting text.

### Current AI Truth correction

The candidate commit path must record separately:

- document/source author under existing document provenance;
- AI worker as expression selector, candidate preparer, and proposed matcher;
- exact analysis run/provider/model and candidate identity;
- exact operation/egress authorization actor and policy basis;
- substantive reviewer, when one actually evaluated the content;
- human semantic reviser when proposition, claim kind, structured meaning,
  applicability scope, or valid time changed;
- evidence-selection actor when the prepared evidence set changed;
- expression-relationship assessor when the prepared expression role changed;
- human candidate-decision actor;
- separate claim lifecycle-decision actor, when one exists;
- Truth kernel as mutation applier; and
- source/evidence premises used.

Canonical producer attribution is decided from the accepted outcome:

| Outcome | Canonical attribution |
|---|---|
| Add an unchanged new candidate | AI is the claim semantic producer and candidate preparer. |
| Add after a human changes proposition, kind, structured meaning, applicability scope, or valid time | Preserve the AI-prepared candidate/derivation; record the human as semantic reviser/co-producer of the accepted claim. |
| Change only evidence attachment | Preserve claim producer attribution; record the human evidence-selection act and exact chosen evidence set. |
| Change only how the passage expresses the claim (`expression_role`) | Preserve claim producer attribution; record the human expression-relationship assessment and corrected role. |
| Connect to an existing claim | Preserve the existing claim's original producer/history. Record AI only as candidate preparer/matcher/selector and the human as connector/candidate decision actor. |

An edit that changes candidate identity is re-matched before commit; it cannot
silently reuse the AI's stale exact/equivalent-match conclusion.

The canonical/exported Truth history must contain the human candidate decision,
not only the private analysis runtime. **Adding or connecting a candidate does
not confirm the proposition.** Candidate decisions (`add`, `connect`,
`dismiss`), claim lifecycle decisions (`confirm`, `challenge`, `reject`),
document review decisions, and any new human assertion are distinct bound
records. A UI may deliberately combine two only by showing and fingerprinting
both consequences. The existing human-only general
`connect_claim` API remains; a narrower multi-actor candidate-commit kernel
handles staged AI output without weakening general authorization.

The source-backed candidate contract preserves **applicability scope** and
**valid time** (`valid_from`/`valid_to`) already supported by Truth, alongside
claim kind, proposition, structured claim data, and expression role. AI may
propose those fields, but they remain
independently visible and editable before the candidate decision and are
included in the exact decision and confirmation fingerprints. “In this
manuscript,” “for this paragraph,” “generally,” and “until this date” must not
collapse into one global enduring preference. A later changed preference uses
explicit supersession/valid-time history rather than silently rewriting the
earlier statement. Assertion/recording time remains distinct from the time
during which the proposition is said to hold.

### Claim–evidence policy

All new positive and nonpositive assessments use one
`link_type = evidence_relation` and validated `role_json` schema
`claim-evidence/v1`, with separate evidential-effect and derivation axes.
Existing `supports_span` links remain untouched and project as compatible legacy
positive support when otherwise usable; new code does not create them. Unknown
or ad hoc legacy role JSON stays opaque/`legacy_unspecified`. Only validated
`supports` and policy-approved `partially_supports` effects count toward usable
support policy.
Profiles gain per-claim-kind policy such as:

```json
{
  "minimum_usable_supports": 1,
  "allowed_effects": ["supports", "partially_supports"],
  "allow_human_assertion_as_source": true
}
```

The compatibility default remains zero required supports. Stricter policies
are opt-in and migration never invents roles for existing links.

Optional selection-intent, semantic-assessment, and claim-extraction confidence
are stored separately with producer/model/calibration metadata. They are
diagnostics, never authority, exactness, or claim status.

## 7. Co-work integration

### Domain-document bindings

The owning domain record is authoritative for its current Co-work binding.
Co-work stores a reverse-binding mirror for portability, query, and repair:

- binding ID;
- domain namespace/kind/entity ID;
- Co-work store/document ID;
- role (`body`, `daily_log`, `running_note`, `task_note`, etc.);
- created/superseded times and actors;
- migration/import origin; and
- uniqueness and lifecycle policy.

`document_class` remains a document behavior classification, not an overloaded
domain foreign key. `DocumentChangeRecord` lives in the Co-work/Truth sidecar
and participates in document export, compaction invariants, and repair.

Use one deterministic **vault-owned domain-content Co-work store** per vault,
registered independently of whichever project folder happens to be active.
Initial document granularity is:

- one document per stable Running Note;
- one managed Log document per logical Journal day;
- one document per task-note UUID; and
- no Co-work document for schedule/planner state.

Daily Markdown sections Work Buddy does not own are preserved verbatim. Managed
sections use section-level base hashes/CAS during projection.

### Document change records

Every controlled mutation produces a durable record containing:

- operation/change ID and kind;
- document/store identity;
- base and result structured heads/generations;
- base and result projection hashes;
- Yjs update hash and schema/runtime version;
- changed selector/range and exact before/after digests;
- content author/inputter where supported;
- selected/produced/applied/reviewed actors;
- source references and exact copied-content digest;
- proposal/review/candidate identities;
- assurance per asserted property; and
- idempotency/effect identity.

Direct edits, exact source copies, imports, AI proposals, and accepted revisions
are distinct change kinds.

### Structured-document runtime decision

Current Python cannot construct or interpret Yjs/ProseMirror state. The
recommended solution is a small versioned headless TypeScript structured-
document runtime that shares a DOM-free kernel schema, Markdown projection, and
bootstrap/mutation package with the React editor. Browser/UI extensions such as
menus and decorations are not loaded into this kernel.

It should expose a narrow local protocol:

- bootstrap canonical structured document from Markdown/source;
- apply a typed source-backed insert/replace operation;
- materialize canonical Markdown;
- return bound before/after hashes, update bytes, distinct source/destination
  selectors, and an operation manifest; and
- reject schema/runtime-version mismatch.

The TypeScript runtime is a trusted document-kernel component and performs
structural interpretation/validation. Python controls authorization,
idempotency, source resolution, exact observable source equality, protocol and
runtime-version binding, limits, CAS, and durable persistence. Python does not
claim an independent interpretation of opaque Yjs structure merely because it
rechecks runtime-produced hashes.

The runtime needs production Node packaging, managed-process lifecycle, health
checks, hidden restart under sidecar reset, bounded request deadlines,
cancellation/ambiguous-completion reconciliation, and a coordinated
browser/server/runtime version rollout. Prototype the DOM-free bundle in plain
production Node before accepting its ADR.

This avoids maintaining a second ProseMirror/Yjs-to-Markdown implementation in
Python. A client-generated fallback can remain for interactive editing, but it
is recorded as `trusted_surface_attested` unless the trusted document kernel
reproduces/checks the asserted structural relationship.

## 8. Journal integration

### First vertical slice

Quick Capture preserves the existing contract:

- `day_id` resolved/validated under authoritative logical-day/timezone policy;
- `target_id = auto | log | running_notes`;
- `mode = dumb | smart` (`auto` requires `smart`);
- `exact_text`; and
- optional `stated_at` plus client mutation ID.

There is no `both` target. `auto` classification is one independently retryable
effect that resolves to one destination. Direct Log/Running Notes writes do not
require AI. Smart processing after a direct destination is a separate effect
that never rewrites the exact captured input.

One source transaction commits:

- `SourceItem` / human input event;
- generic immutable ingress submission and versioned Journal command envelope;
- idempotency result; and
- outbox effect rows.

Journal owns its capture/entry/effect record. A new `JournalContentAdapter`
provides read/snapshot, `append_log`, and `append_running_note` operations and
is the only PR 1 writer for capture materialization. Each new structured Journal
entry has a deterministic ID; its Markdown projection carries a parser-
recognizable hidden marker and digest so a crash after file write but before
acknowledgement can reconcile without duplicate append.

The production provider is initially hybrid: it composes authoritative legacy
Today/timeline data with real capture/Log/Running Notes records. Demo-only
widgets are hidden or explicitly unavailable; the app does not switch away
from the demo provider until the rendered capability model is honest and passes
provider conformance. The provider reconciles by client mutation ID and shows
separate persisted and processing state.

Preexisting unmarked Running Notes do not receive placeholder or line-derived
IDs in the stable Running Note widget. They remain a separate read-only legacy
compatibility projection/notice until an explicit migration assigns reviewed
stable identity.

The source occurrence is canonical source history; current Journal Markdown
remains the domain composition authority until each entity's explicit authority
epoch cutover (one Running Note pilot in PR 3, scaled migration in PR 4).

### Eventual content model

Journal keeps structured capture, date, route, note identity/version,
tombstone, and lifecycle records. Long-form/daily prose moves into bound
Co-work documents after the structured runtime lands. Markdown remains
intentionally editable for existing Obsidian workflows, but it is no longer
silently co-authoritative: every materialization stores a projection base
hash/generation; divergence is never overwritten. An external edit is captured
as an exact file-origin source and enters a controlled import/review flow before
a new projection is emitted.

After an entity cuts to Co-work authority, a domain projection worker keeps its
Markdown compatibility view current for **every committed Co-work document
head**, including ordinary direct editor changes. The worker enumerates
authoritative domain bindings and compares the durable Co-work head with the
domain-local projection cursor; the lossy event stream is only a wake-up hint.
Its idempotency key is `(binding_id, content_authority_epoch,
document_head)`. It records prepared state, renders through the shared document
kernel, section-CASes the managed Markdown marker against its base hash, and
commits a projection receipt/cursor. A crash or ambiguous file write reconciles
from the marker and digest. If the file diverged, the worker captures the exact
file-origin source, pauses that entity, and requires import/review rather than
overwriting it.

Authority epochs are per stable Running Note and per logical-day Log—not per
rollout cohort. Cohorts control rollout only. The same projection pattern later
applies per task-note UUID.

## 9. Task integration

Task master rows and scheduling remain in Tasks/Obsidian Tasks. Only task note
bodies are candidates for bound Co-work documents.

Introduce `TaskNoteContentAdapter` before migration so every mutation, IR
source, context reader, and observability collector reads through one seam.
Then:

1. inventory and shadow-import note bodies;
2. bind each stable `note_uuid` to a Co-work document;
3. compare normalized content and provenance;
4. dual-read with authoritative-old fallback, but do not dual-write as peers;
5. cut write authority to Co-work;
6. materialize Markdown for compatibility; and
7. retain measured rollback until parity is proven.

Task-note projections use the same base-hash/generation and non-clobbering
external-edit import semantics. Creation, deletion, retirement, and recovery are
explicit sagas spanning task metadata, note binding/document, projection, and
existing retry behavior.

## 10. Ordinary direct Co-work editing

Ordinary editor transactions do not each become a global source item and the
system does not promise per-keystroke semantic authorship. They are batched at
the existing durable update/sitting boundary into change records carrying the
requesting local principal, update hash, heads, and the defensible trusted-
surface assurance. Controlled exact source insertions, imports, and accepted
proposals carry richer source/change records because those operations have
explicit inputs and review boundaries.

## 11. Failure and freshness semantics

The public state machine separates source persistence from effects:

```text
draft -> persisting -> persisted
                     -> processing -> succeeded
                                   -> partially_succeeded
                                   -> failed_retryable
                                   -> failed_terminal
```

A persisted source is never presented as lost because one effect failed.
Retries use the same effect ID. Partial multi-effect completion is explicit.

Source freshness has three independent projections:

- **snapshot integrity:** retained bytes still match the capture digest;
- **capture-time resolution:** what the resolver established when captured;
- **current origin state:** unchanged, changed, unavailable, mismatch, or not
  checked.

First-party canonical human-input items generally need snapshot integrity, not
origin polling. External/native origins may have policy-driven rechecks.

## 12. Export/import and derived projections

Sources export records the originating authority ID, items, representations,
attributions, observations, derivations, usage/issuance state, and redaction
state under explicit authorization. One local database may custody records
minted by several authorities: its own `local_authority_id` is the only
authority under which normal ingress may mint new items, while imported foreign
authorities are read-only namespaces admitted only through validated import.
The source-item key is `(minting_authority_id, item_id)`, and the store records
both minting authority and current custodian.

Import preserves an original `SourceRef`
when the same identity and canonical payload agree; a conflicting identity is
quarantined. Any explicit local remap is recorded in the import manifest and
retains the original reference. Actor issuers are mapped explicitly or remain
foreign/unknown; access bindings never silently import as local grants.
Pending/retryable outbox work imports suspended and inert; a deliberate local
re-authorization/reconciliation step is required before it can execute.

Hindsight/memory remains a derived projection, never authority. Only policy-
eligible current Truth claims are projected with a claim reference and status;
summaries are never labeled verbatim user statements. Rejection, supersession,
expiry, and redaction invalidate/update the projection idempotently.
The projection registers a semantic-derivative usage/dependency so source or
claim redaction never disappears from maintenance accounting merely because the
wording changed.

Truth owns this projection lifecycle. The same Truth transaction that changes
an eligibility-relevant claim state writes a content-minimized projection-
outbox row keyed by claim, generation, and desired projection state. An
idempotent Hindsight consumer records a destination receipt/cursor, while a
deterministic Truth-to-Hindsight reconciliation sweep repairs missed or
ambiguous delivery and removes projections that are no longer eligible.
Sources records the derivative dependency for redaction accounting but does
not own or deliver Truth lifecycle changes. The general event stream may wake
the consumer; it is not the authoritative delivery record.

## 13. Security and privacy boundary

- The Sources API is local-machine/tenant scoped and purpose-authorized.
- Resolver implementations defend against path traversal, symlink escapes,
  provider confusion, SSRF, unsafe redirects, and oversized content according
  to source kind.
- Generic telemetry records IDs, digests, byte counts, state, and typed error
  codes—not raw content.
- Exports are explicit and authorization-scoped.
- Every background consumer validates the exact effect's durable narrow
  authorization basis or pauses for a fresh user decision; it never inherits a
  long-expired session/workflow grant on replay.
- Redaction and restore are tested together; no “append-only” claim excuses a
  readable stale export or backup projection.
- Current vital backups may be long-retained and remotely stored; Sources cannot
  enter production backup until encryption, retention, redaction-manifest, and
  restore fencing are explicitly implemented. A restored pre-redaction snapshot
  must consume an external current redaction manifest or equivalent key-erasure
  boundary before readable state is exposed.
