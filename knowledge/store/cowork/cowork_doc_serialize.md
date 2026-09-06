---
name: Cowork Doc Serialize
kind: capability
description: Return one cowork doc's canonical Markdown at its current head, with the head and projection digests that identify it.
capability_name: cowork_doc_serialize
category: cowork
op: op.wb.cowork_doc_serialize
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
- serialize
- projection
aliases:
- read cowork document text
- cowork document markdown
- project cowork document
- get document content
parents:
- cowork
---

`cowork_doc_serialize` is the read that returns document *text*. Its sibling
`cowork_doc_get` returns metadata and the review layer and deliberately carries
no content, because content otherwise rides the binary Y.Doc transport and is
reachable only inside the browser.

## What the text is

The **canonical projection**: exactly what the browser's own Markdown serializer
produces for the same state. That matters beyond fidelity, because it is the
coordinate space in which proposal anchors, expression marks, and content hashes
resolve. A caller comparing offsets, placing a selector, or hashing content is
only correct against this text.

The projection covers the compacted snapshot **plus every un-compacted update**,
so it reflects edits that have not yet been folded into a snapshot.

## The canonical form is not publication shaping

Canonical Markdown encodes `&`, `<`, and `>` as entity references and
backslash-escapes Markdown-significant punctuation outside code spans and code
blocks. That is part of the form, not damage to be repaired:

- It is valid CommonMark, and every renderer decodes it. A converter emits
  `Abbas, M., & Khan` from `Abbas, M., &amp; Khan`.
- The escaping is what keeps a document's literal text literal. Removing it can
  turn prose into markup, an author's typed `&amp;` into `&`, or an inert
  bracket into an active citation.

Convert from this text. Do not rewrite it first.

## Reading the response

| Field | Meaning |
|---|---|
| `markdown` | The canonical projection. |
| `structured_head_sha256` | The head this text was produced from. Pin it when the text will be sent somewhere, so the recipient's copy is identifiable later. |
| `projection_sha256` | Digest of these exact bytes. |
| `byte_length` | UTF-8 length of `markdown`. |
| `projection_receipt_match` | Whether the stored projection blob agrees with what the worker just produced. `null` when no receipt-bound projection is available to compare. |

`projection_receipt_match: false` is the observable form of a serializer
disagreement between the packaged worker and the browser. It is reported rather
than raised so that a caller can decide whether the difference matters for what
it is doing.

## Failure modes

- A document with no structured snapshot cannot be projected.
- An in-flight compaction raises rather than returning text. Compaction rotates
  the update log separately from the snapshot pointer, so a head derived from an
  unchecked pair can name state that no longer exists. Retry.

## Scope

Ordinary agent sessions reach this capability by default. It is deliberately
absent from the hosted document agent's execution set: that agent is
generation-fenced and grounds its work in frozen action snapshots with
consumption receipts, which a live-head read would quietly bypass.
