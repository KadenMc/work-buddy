---
name: Cowork Doc Get
kind: capability
description: Read one cowork doc's metadata, source-writeback policy, hashes, open proposals, expressions, feedback, and drift.
capability_name: cowork_doc_get
category: cowork
op: op.wb.cowork_doc_get
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  document_id:
    type: str
    description: Target cowork doc id.
    required: true
mutates_state: false
retry_policy: manual
auto_retry: false
tags:
- cowork
- doc
- get
aliases:
- read cowork doc
- open cowork document
- get document review layer
- inspect cowork doc
parents:
- cowork
---

`cowork_doc_get` is a read-only view of the document's metadata and
ledger-canonical review layer; content itself rides the binary Y.Doc transport.
It returns open proposals, expressions, feedback, source-writeback policy, and
separately named structured, projection, recorded-import-source, and
currently-observed-file hashes. The read does not append a drift event.

For `source_writeback=never`, the selected import file is a source artifact,
not the live managed projection or a Save target:

- `import_source_sha256`, returned both at top level and in `hashes`, is the
  digest recorded for the captured import source when available.
- `observed_source_file_sha256`, also returned at both levels, is the digest
  currently observed at that source path when safe, bounded observation succeeds;
  otherwise it is `null`.
- `hashes.source_file_sha256` remains a compatibility alias for the observed
  value; it is not the recorded import identity.
- `hashes.current_file_sha256` is the managed-projection baseline for a detached
  import, not a claim about the current source file.

Detached-source observation uses the persisted importer descriptor and its size
limit, accepts only a regular file, and never follows links or reparse points.
An unavailable, unsafe, oversized, importer-unbound, or concurrently changed
source therefore does not fail this routine metadata and review read: its
observed digest becomes `null`, while the managed structured head and projection
remain available.
Requesting the external source explicitly with `version=current` preserves the
distinction by returning a typed failure such as `source_too_large`,
`source_unavailable`, or `source_not_found`.

Detached-source drift is projected as clean and no external-file diff is
offered. A changed observed digest is still visible so a caller handling a
later **From file** selection can warn and offer the existing managed copy
without silently refreshing it. A writeback-enabled document retains the
ordinary file-drift behavior.

The read returns source digests rather than source bytes. Current imports retain
the exact source bytes in the content-addressed store and portable Truth export;
historical imports may remain hash-only.

The dashboard document-read route uses the same read boundary for the Review
rail and editor annotation projection. It assembles document metadata,
proposals, expressions, claims, provenance spans, and authorship and
human-review attestations from one explicit SQLite snapshot, so the response
cannot combine ledger states from different instants.

Proposal, expression, and provenance spans include complete `exact`, `prefix`,
and `suffix` quote anchors. For an expression whose claim reference resolves to
a local claim (including a canonical same-store `wb-truth:` URI), the expression
entry includes the claim's current `claim_status` and `claim_kind`. External,
malformed, or otherwise unresolvable references return `null` for that metadata
instead of promoting an unknown claim or failing the document read.

Provenance classification is conservative. `ai_confirmed` requires the durable
combination of agent-authored proposal provenance and a human-accepted
replacement span. An authorship and human-review attestation for imported or
pasted text is returned separately in `authorship_attestations`; “AI-written,
human-reviewed” does not become `ai_confirmed`. Each entry retains its source,
basis, attester, person-identity strength, frozen version or span, structured
head, canonical digest, idempotency key, and supersession link. Marking a
passage that already exists does not infer that an agent wrote the prose, and
reporting human review does not verify or certify the content. See
`cowork/content-provenance`.
