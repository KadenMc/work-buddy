# Terminology and invariants

**Status:** Candidate vocabulary recommended for the source foundation. It
becomes golden only after the user accepts the design and the ADR fixture review
passes. Terms marked “local” are deliberately defined Work Buddy concepts, not
claims about an external field-wide standard.

## Preferred terms

| Preferred term | Status | Meaning |
|---|---|---|
| **source item** | Transparent ordinary term | One durable occurrence captured from a trusted ingress or authorized resolver. Identity is not deduplicated by content. |
| **source record** | Acceptable synonym in implementation prose | The persisted representation of a source item. Prefer *source item* in product/design language. |
| **human input event** | Local subtype | A first-party event whose trusted ingress establishes the submitting local principal/gesture and exact payload. Authorship is a separate assertion; paste, import, dictation, automation, and unknown input modes do not become human-authored merely because they used this route. |
| **source snapshot** | Established descriptive phrase | The exact retained bytes/content and digest captured for a source item at a point in time. |
| **source representation** | Established descriptive phrase | One exact representation of a source item: raw bytes, decoded text, ordered multipart content, or a derived canonical text projection, each with its own identity and digest. |
| **SourceRef** | Local value-object name | An authority-qualified stable reference to one retained Sources item. Possession is not authorization. |
| **origin reference** (`origin_ref`) | Transparent ordinary term | Structured native provider coordinates: provider, container, native item ID, revision, part, and similar origin facts. It is separate from `SourceRef`. |
| **source observation** | Local record type using established words | An append-only observation about resolution, integrity, availability, identity, revision, or redaction at a point in time. |
| **resolved source** | Local transient value object | The trusted, content-bearing result of an internal authorized resolution. It stays in-process or behind an authenticated one-time handle; an agent never vouches for it. |
| **source-resolution record** | Transparent record name | The content-bounded or content-free provenance facts a consumer persists from an internal resolution for portability and audit. |
| **source derivation** | Established provenance concept | A typed relationship from one source item to another, such as transcription, translation, quotation, summarization, or revision. |
| **verbatim quotation** | Established phrase | An exact selection from a human-authored speech or writing source. Use *source excerpt* when authorship or medium is generic. |
| **source excerpt** | Transparent ordinary term | Exact selected content from a source snapshot. |
| **claim–evidence relation** | Transparent ordinary term | A validated statement about how an exact evidence span bears on a claim. Only positive qualifying effects are usable support. |
| **source-backed claim proposal** | Transparent composition | An AI- or human-produced proposition tied to exact source evidence and still awaiting the applicable Truth lifecycle decision. |
| **applicability scope** | Transparent ordinary term | The bounded context in which a claim is asserted to apply, such as globally, one project/document, or one passage. It is not an access-control scope. |
| **valid time** (`valid_from`/`valid_to`) | Established temporal-data term | The interval during which the proposition is asserted to hold, distinct from when the record was created or changed. |
| **supersession** | Established descriptive/provenance term | An explicit later claim/version replaces an earlier one for a stated scope/time without rewriting its history. |
| **domain-document binding** | Local record type | A durable relationship between a structured domain entity and a Co-work document. |
| **document change record** | Preferred local term | Durable causality/provenance for one accepted document mutation. Use “receipt” only in APIs that intentionally return an acknowledgement. |
| **trusted ingress** | Security term used descriptively | A server-recognized surface/host path able to establish the exact submitted payload, issuer, and input principal/gesture only to its explicitly stated assurance. It is not automatically authenticated-human proof. |
| **assurance level** | Established descriptive phrase | What mechanism supports an authorship/fidelity/causality assertion. It is not truth confidence. |

Avoid “AI-human verbatim,” “artifact assurance” as an unexplained term of art,
“the user said” without a source basis, and any single undifferentiated
“verified” state.

## Standards alignment without overclaiming

The [W3C PROV Ontology](https://www.w3.org/TR/prov-o/) supplies useful
established distinctions among an entity, an activity, and an agent, and among
attribution, generation, use, derivation, quotation, and revision. Work Buddy
should be mappable to those concepts, but it does not need to expose RDF or
adopt PROV-O as its storage schema.

The [W3C Web Annotation Data Model](https://www.w3.org/TR/annotation-model/)
supplies useful selector and source-state precedents. In particular, a quote
selector can preserve exact text plus prefix/suffix context while a state
describes the source representation to which the selector applied. Those
structures aid anchoring; they do not by themselves prove authorship, identity,
or semantic support.

`prov:wasQuotedFrom` is therefore conceptual prior art for a quotation
derivation. It is not evidence that two strings are character-for-character
equal. Work Buddy establishes exactness by resolving an authorized retained
snapshot and copying the selected bytes/text in the kernel.

## Identity model

### Source reference

The canonical form identifies the retained Sources authority and item, not the
native provider:

```json
{
  "schema": "wb.source-ref/v1",
  "authority_id": "01J...",
  "item_id": "01J..."
}
```

Its canonical URI serialization is:

```text
wb-source://01J.../item/01J...
```

Rules:

- `authority_id` is the persistent opaque identity of the Sources store that
  minted the item. It is not a provider, model, vault path, or user name.
- `item_id` is opaque, server-minted, and stable.
- The URI contains no raw source text, user name, filesystem path, or mutable
  “latest” coordinate.
- Native provider identity and coordinates live in `origin_ref`; they are not
  overloaded into the canonical retained-item identity.
- A source item's content digest is an immutable precondition/property, not
  the source item's occurrence identity.
- Identical bytes from two native messages remain two source items.
- Import normally preserves the original authority-qualified reference. A
  collision with a different canonical item is quarantined rather than
  remapped silently; any necessary remap is explicit in the import manifest and
  retains the original reference as provenance.
- A local store may custody imported foreign-authority rows but can mint new
  items only under its own authority ID.

### Actor reference

An actor reference must be issuer-qualified and scoped:

```json
{
  "schema": "wb.actor-ref/v1",
  "issuer_authority_id": "01J...",
  "subject": "dashboard-user",
  "kind": "human",
  "tenant_scope_id": "01J..."
}
```

`issuer_authority_id` and `tenant_scope_id` are persistent opaque authority
identities, not generic labels such as `work-buddy-local` or `machine-local`.
The initial single-user implementation can use a local subject, but the
authority-qualified tuple must not make `dashboard-user` globally unique or
assume that all future collaborators share one identity. Assurance belongs to a
particular attribution assertion, not the stable actor identity. An attribution
records its basis, assurance, asserting actor/component, and observed time.

Source authorship is an append-only set of attribution assertions with a
current projection. It may be unknown, mixed, or identify several authors.
Corrections and human attestations supersede assertions; they do not rewrite the
captured source item.

## Independent actor dimensions

| Dimension | Question answered | Example |
|---|---|---|
| **source author** | Who authored the retained content? | A named collaborator, unknown external author, software agent. |
| **inputter** | Who supplied it to this surface? | The local human pasted a quotation authored elsewhere. |
| **trusted issuer / captured by** | Which trusted component established the record? | Dashboard Quick Capture route. |
| **selected by** | Who chose this exact excerpt? | AI analysis worker. |
| **candidate prepared by** | Who produced the staged candidate offered for review? | AI analysis worker. |
| **semantic producer** | Who formulated the proposition or interpretation? | Claude Code · Sonnet worker. |
| **semantic reviser** | Who materially changed proposition, kind, applicability scope, or valid time? | Human editing an AI-prepared candidate. |
| **matched by** | Who proposed that a candidate corresponds to an existing claim? | AI analysis worker. |
| **evidence selected by** | Who chose which evidence candidates to attach? | Human reviewer changing the prepared selection. |
| **expression relationship assessed by** | Who classified how a passage expresses a claim (for example quote or paraphrase)? | AI initially, then a human who corrects the role. |
| **applied by** | Who or what performed the domain mutation? | Truth kernel or Co-work structured runtime. |
| **execution authorized by** | Who/what consented to this exact operation, egress, or cost? | User-bound authorization or applicable policy grant. |
| **substantively reviewed by** | Who evaluated the content/evidence itself? | Local human reviewer. |
| **candidate decision by** | Who chose to add, connect, edit, or dismiss prepared work? | Local human decision actor. |
| **lifecycle decision by** | Who confirmed, challenged, rejected, or otherwise changed a claim's standing? | Local human under Truth policy. |
| **attested by** | Who asserted a fact the kernel could not independently verify? | Trusted editor surface. |

No field may silently substitute for another. In particular:

- a human acceptance is not human authorship of an AI-formulated claim;
- an agent acquisition is not source authorship;
- a provider's `role=user` is not verified local-human identity;
- the actor who compacts a Y.Doc is not the author of all resulting content;
- a person pasting text is the inputter, not necessarily its author.

## Authorship and fidelity cases

| Ingress | Default content authorship | Fidelity |
|---|---|---|
| First-party submission under a trusted local gesture/principal | Human is the inputter. Authorship is human only when a separately recorded direct-entry/user attestation supports it; otherwise unknown. | Exact retained payload |
| Paste/import | Unknown or explicitly attested author; human is inputter | Exact retained supplied bytes/text |
| Native text conversation message from a trusted provider resolver | Provider-reported author plus identity assurance | Exact only if raw native item is retained without normalization |
| Voice recording | Speaker identity at its supported assurance | Exact audio bytes |
| Speech-to-text transcript | Machine-produced derivative attributed to transcriber/model | Derived from audio; not verbatim by default |
| AI-generated text | Software agent/model run | Exact retained output |
| AI rewrite of a human source | Mixed/AI-derived | Not an exact quotation |
| Exact source-backed insertion | Inherits source authorship for copied text only | Exact after kernel comparison |

## Claim–evidence relations

The relation has two independent axes:

1. **Evidential effect:** `supports`, `partially_supports`, `contradicts`,
   `mentions`, `does_not_address`, or `inconclusive`.
2. **Derivation relationship:** `direct_statement`, `paraphrase`, `inference`,
   or `context`.

`synthesis` is a claim-level derivation method across several premises, not a
property of one evidence-to-claim edge.

A relation may also retain separate optional selection-intent,
semantic-assessment, and claim-extraction confidence values with producer/model
and calibration metadata. These are fallible diagnostics—not authorization,
source identity, exact-quote fidelity, or Truth status.

Only `supports` and policy-approved `partially_supports` edges count as usable
support. The enum names remain proposed until tested against a representative
fixture set. Unknown or legacy edges remain `legacy_unspecified`; migration
must never guess.

## Assurance levels for document changes

| Level | Meaning |
|---|---|
| `document_kernel_verified` | The trusted headless document kernel interpreted the structured document and checked the claimed structural relationship. |
| `persistence_verified` | Python independently checked authorization, request/result binding, expected head/CAS, sizes, hashes, and exact observable source bytes before durable commit. |
| `projection_verified` | The trusted document kernel produced the bound before/after canonical projections and operation manifest; Python verified their persistence binding, not the opaque structure independently. |
| `trusted_surface_attested` | A trusted client supplied an attestation the opaque server could not independently derive. |
| `user_attested` | A human explicitly asserted authorship/source/review. |
| `inferred` | A model or heuristic inferred the relationship. |
| `unknown` | No stronger defensible statement is available. |

Assurance is always attached to a particular assertion. A single change can be
`projection_verified` for exact inserted text and only
`trusted_surface_attested` for structural node identity.

## Non-negotiable invariants

1. Exact input is durable before AI processing or domain mutation.
2. The model may select and interpret a source; the kernel resolves and copies
   the authoritative excerpt.
3. `SourceRef` is retained-item identity, not origin identity or permission.
4. Source identity, snapshot integrity, current resolvability, authorship,
   semantic support, and claim status remain separate axes.
5. Capture-time success survives later origin unavailability.
6. A later changed, missing, redacted, or identity-mismatched origin appends an
   observation; it does not rewrite capture history.
7. Source items are immutable except for recorded tombstoning and
   application-level readable-content deletion under a redaction event and
   declared storage threat model.
8. Selectors bind an exact representation ID and state/digest; raw bytes,
   decoded text, multipart content, and canonical projections are not
   interchangeable.
9. Source and domain operations are idempotent: same key/same payload returns
   the original result; same key/different payload conflicts.
10. Raw content never enters IDs, idempotency keys, generic event payloads,
   error messages, or operation logs.
11. Downstream effects are independently idempotent and delivered from a
    durable source-owned outbox.
12. The general agent API cannot assign human authorship. A trusted-surface
    attestation states only what that local boundary can substantiate.
13. A source-backed Truth operation is atomic within Truth even though source
    capture and Truth mutation are separate transactions.
14. Adding or connecting an AI candidate is separate from confirming the claim
    as true; any intentionally combined gesture records two bound decisions.
15. AI-produced claims begin proposed; exact quotation does not make a
    proposition true.
16. Claim confirmation policy is claim-kind/profile-specific; zero evidence
    remains a deliberate compatibility policy, not an accidental global rule.
17. Truth, Co-work, Journal, and Tasks retain their own lifecycle authority.
18. Markdown and Co-work cannot remain co-authoritative indefinitely after a
    domain cutover.
19. Opaque Yjs updates never support stronger authorship claims than the
    verification mechanism actually establishes.
20. Redaction tracks both readable copies and semantic derivatives that may
    disclose source content. A crash-safe usage handshake and domain sweeps—not
    a best-effort central index alone—establish managed-copy coverage.
21. A failed cross-domain effect does not erase a successfully persisted source
    item; it remains visible as pending or failed and is retryable.
22. Backfills do not guess provider, actor, authorship, support role, or stable
    native item identity.
23. AI extraction never silently broadens claim applicability scope or valid
    time; those fields are visible, independently editable, and bound into the
    candidate and lifecycle decisions.
24. A human-authority mutation requires an issuer-qualified persistent
    principal plus an exact bound gesture under the declared threat model;
    request headers and same-origin routing are insufficient.
25. After a domain entity cuts to Co-work authority, its compatibility
    projection follows every durable document head and pauses rather than
    overwriting external divergence.
26. Canonical producer attribution follows the accepted outcome: a human edit
    to proposition/kind/structured meaning/scope/time records semantic revision,
    evidence changes record evidence selection, expression-role changes record
    relation assessment, and connecting an existing claim never replaces that
    claim's original producer with the candidate preparer.
27. Agent Execution owns model runs. Every Work Buddy-managed content-bearing
    prompt/tool response and every model-produced external tool argument is
    recorded directionally in a run-scoped multi-source disclosure manifest.
    `possibly_sent` is persisted before the irreversible send and is never
    automatically replayed.
