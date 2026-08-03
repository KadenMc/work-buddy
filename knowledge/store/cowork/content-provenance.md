---
name: Co-work Content Provenance
kind: concept
description: Frozen-target, append-only attestations that keep content source, authorship, human review, and the attester distinct.
summary: >-
  From file records source facts and an authorship and human-review
  determination against the imported document version. Text-bearing pastes
  record the same dimensions against an exact span: substantial or structured
  text asks the user, while short simple text uses an explicit
  automatic-attribution basis. These attestations report what a person says
  about the content. They do not verify authorship, correctness, or claims.
tags:
- cowork
- provenance
- authorship
- human-review
- documents
- append-only
aliases:
- content provenance
- authorship attestation
- human review attestation
- document provenance attestation
parents:
- cowork
---

# Co-work content provenance

Co-work records four related facts without collapsing them:

- **Source** says how the content entered Co-work, such as a file import, paste,
  direct entry, or accepted proposal.
- **Authorship** is human, AI, mixed, or unknown. Human or mixed authorship can
  name human contributors.
- **Human review** is reviewed, not reviewed, not applicable, or unknown.
  Reviewed content can name its human reviewers.
- **Attester and basis** say who supplied the information and whether it came
  from a user attestation, automatic short-text attribution, proposal
  acceptance, migration, or legacy data.

An authorship and human-review attestation is a report, not a verification
result. Saying that a person reviewed AI-written text does not mean the text was
approved, fact-checked, accepted as correct, or adopted as the person's own
writing. Accepted-proposal provenance remains a separate stronger chain:
Co-work knows the producing agent run, the exact proposed wording, and the
human acceptance gesture.

## Frozen targets

Every attestation is bound to content that cannot silently change underneath
it:

- **From file** targets the immutable document version created by the import and
  records that version's structured-head hash.
- **Pasted text** creates an exact quote-anchored document span and binds the
  attestation to that span and one expected structured-head digest. The client
  first persists the inserted edit, then freezes that digest into the request;
  the server records the attestation only if its locked current head still
  matches.

Before sending or replaying a paste request, the client requires the complete
`exact`, `prefix`, and `suffix` quote anchor to resolve to exactly one passage in
the currently hydrated editor. An absent or ambiguous passage becomes a stale
target instead of being silently attached elsewhere. The server also binds an
idempotency key to the exact selector, attestation, and structured head, so an
ambiguous response can replay only the same immutable logical request.

The append-only record carries a canonical digest and idempotency key.
Corrections append a replacement that names the prior attestation through
`supersedes_id`; they do not update or delete history.

## From file

**From file** is format-neutral at the picker and importer-registry boundary.
The only importer currently registered is `markdown/v1` for `.md` and
`.markdown` files with the `text/markdown` media type. A later Word importer can
join the registry without changing the outer workflow or provenance model. An
importer owns its accepted paths, media type, title derivation, source-size
limit, and conversion into the structured Co-work representation; the current
Markdown limit is 16 MiB.

The server registry is authoritative for admission and returns a validated,
versioned importer descriptor. The browser selects a bundled converter only by
that exact importer ID and uses the descriptor's suffixes only for presentation,
such as title derivation. If the server admits an importer version the browser
does not implement, the import stops with
`importer_version_unavailable` before document commit; the browser never guesses
a converter from the filename or media-type claim.

Later observation of a detached source also uses the document's persisted
importer descriptor and its source-size limit. Co-work opens only a regular file
without following links or reparse points, hashes it within that bound, and
rejects a source whose identity changes during the read. Routine catalog,
document, and drift views reduce an unsafe, unavailable, changed, or oversized
observation to an unknown digest (`null`) without making the managed document
unusable. An explicit current-source read instead returns a typed failure such
as `source_too_large` or `source_unavailable`. Historical pre-registry Markdown
imports may use the same bounded `markdown/v1` rules; Co-work does not guess a
different importer.

The Markdown importer performs supported import normalization into the
structured editor model. The exact source artifact hash and the managed
projection hash are recorded separately, because normalized formatting can make
their bytes differ. A current import also retains the exact selected bytes as a
content-addressed source blob: when source and projection match they share that
blob, and when they differ the source is retained independently. Portable Truth
export includes those exact bytes when captured. Historical imports upgraded
from before source-byte retention may remain hash-only; the missing source blob
is an integrity warning rather than a reason to make the document unusable.

Imported source metadata carries `writeback_policy=never`. The original file is
never rewritten by editing, proposal acceptance, Save, retirement, recovery, or
portable import; Co-work advances its own structured state and managed
projection. Before a detached import is retired, the live editor retries pending
persistence, flushes its outbox, verifies the canonical head, and compacts any
Yjs update tail into a durable snapshot. This is an internal lifecycle
settlement, not a file materialization, and it leaves the source bytes unchanged.

Selecting a path already registered as a detached import opens its existing
managed Co-work copy automatically only when the newly observed source hash
matches the recorded import hash. If the source changed, or a historical record
lacks enough identity to make that comparison, Co-work warns and offers **Open
existing Co-work copy**. That action neither refreshes the managed copy from the
selected file nor changes the file. Re-selection never silently converts an
external file change into replacement document content.

Retirement permanently reserves the document's original path identity so its
history cannot later be confused with a new document. Selecting that exact
source path again returns a typed retired-path conflict and offers **Choose
another file**, not an impossible action for opening the retired document.
Copying or renaming the source creates a distinct path that can receive a new
document identity. The conflict is decided from registered identity before the
source is read or written, whether or not the source bytes changed after the
original import.

Before the import commits, the shared provenance form asks who wrote the
content and, for AI or mixed content, whether a person reviewed it. The same
form is used when a paste is large or structured enough that direct human
authorship should not be assumed. A short ordinary paste may be attributed to
the current user automatically, with that automatic basis recorded explicitly.

## Pasted text

This first paste-provenance slice covers text-bearing editor paste
transactions. It anchors the text that the editor actually inserted after
ProseMirror normalization, including the text in supported rich clipboard
content. It does not attest image-only or attachment-only clipboard content,
preserve the original clipboard HTML, or infer where the clipboard content
originated.

A paste asks for the shared provenance determination when it has more than one
top-level block, contains a list, task list, code block, blockquote, or table, or
contains at least 600 Unicode characters in one ordinary block. The text is
inserted immediately; the modal determines authorship and, for AI or mixed
authorship, whether and by whom it was reviewed. Choosing **Decide later**
records unknown authorship rather than inventing an author.

A single paste is bounded to 1,000,000 Unicode characters so its exact quote
anchor remains admissible to the provenance ledger. A larger paste is rejected
before it enters Yjs, the recovery journal, or the outbox, and the editor asks
the user to paste it in smaller sections. This prevents an edit from becoming
durable while its required provenance record is permanently undeliverable.

A text-bearing paste below that threshold, with one ordinary block and no
listed complex structure, is automatically attributed to the current local
human actor with review marked not applicable. This is a user-requested
low-friction heuristic based on paste shape, not proof that the user wrote the
clipboard content; its record carries
`basis=automatic_short_text_attribution` so downstream readers can distinguish
it from an explicit user determination.

The browser keeps pending paste records in a document-scoped IndexedDB FIFO
outbox.
Before the asynchronous outbox write, it synchronously stages the capture in a
small local-storage recovery journal; hydration reconciles staged captures into
the outbox and deduplicates them by idempotency key. Once a determination is
ready, Co-work flushes the Yjs edit, freezes the complete request against the
resulting structured head, revalidates the unique quote anchor, and retains that
same request until the server confirms receipt. Retryable, terminal, and stale
failures remain explicit and recoverable.

The Yjs edit, local recovery journal, IndexedDB outbox, and Truth attestation do
not form one atomic transaction. The synchronous journal is a recovery barrier
for that cross-store gap, not a claim of atomicity. If browser storage itself
fails, Co-work keeps the capture in the mounted page, warns the user to keep the
page open, and offers a storage retry. Malformed stored records are quarantined
individually while valid records remain usable; a transient IndexedDB startup
failure can be retried without remounting the page.

## Person identity

**Me** first obtains the current actor binding from the server and freezes its
ref and identity status into the determination. The server revalidates that
binding when the import or paste is recorded; if the acting identity changed,
the frozen determination is rejected instead of being reassigned to the new
actor. For queued pastes, the browser refetches the current actor, clears the
stale frozen requests, rotates their idempotency keys, and changes every pending
determination to unknown authorship with an explicit user-attestation basis.
Nothing is resent until the user makes a fresh determination, including a short
paste that was originally eligible for automatic attribution.

The local actor ref is durable within the local system, but the dashboard has
no authenticated multi-user boundary. It is stored as
`identity_status=local_actor_ref` and must not be described as a verified
account identity.

**Someone else** stores the typed display name with
`identity_status=claimed_name`. It is useful attribution supplied by the
attester, not proof of identity. The Truth schema reserves
`identity_status=account_ref` for a future authenticated participant or account
reference. The current dashboard does not mint that status. Future
collaboration can supply it from a participant directory while retaining the
same authorship, reviewer, attester, and frozen-target fields.

## Storage and portability

Truth schema v8 stores these facts in the append-only
`document_provenance_attestations` table. Truth export format v8 includes the
records, validates their target links and canonical hashes on import, and
preserves supersession history. It also includes exact retained import-source
blobs when available while accepting that historical imports can carry only a
source hash. Existing detached imports from before provenance attestations
receive a deterministic migration-backfill attestation with unknown authorship,
unknown human-review status, and a system attester; migration does not invent a
human or AI author. The managed Markdown projection, browser paste outbox, and
editor decorations remain supporting projections or delivery state; the
append-only Truth record is the provenance authority after receipt.
