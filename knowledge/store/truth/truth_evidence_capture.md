---
name: Truth Evidence Capture
kind: capability
description: Validate a source locator and capture immutable evidence with engine-assigned trust and authoritative agent provenance.
capability_name: truth_evidence_capture
category: truth
op: op.wb.truth_evidence_capture
schema_version: wb-capability/v1
parameters:
  store_id:
    type: str
    description: Registered Truth store identity.
    required: true
  kind:
    type: str
    description: Evidence kind such as document, web, chat, utterance, artifact, or import.
    required: true
  source_locator:
    type: str
    description: Verifiable named-scheme locator for the source.
    required: true
  acquisition_method:
    type: str
    description: Capture method such as fetch, paste, import, said_in_chat, or file_read.
    required: true
  producer_model:
    type: str
    description: Required model claim for the agent authoring this write. It must match a non-placeholder session-manifest model; otherwise it is durably labeled caller_asserted, not authenticated.
    required: true
  content:
    type: str
    description: Optional captured text. Supply content_sha256 for hash-only evidence.
    required: false
  content_sha256:
    type: str
    description: Optional SHA-256 digest, required when content is omitted.
    required: false
  media_type:
    type: str
    description: Optional MIME media type.
    required: false
  acquired_at:
    type: str
    description: Optional ISO 8601 acquisition time.
    required: false
  origin:
    type: str
    description: Optional acquisition origin used by engine trust assignment.
    required: false
  external_reviewed:
    type: bool
    description: Whether a human reviewed external evidence. Agents cannot set this true.
    required: false
  derived_from_store:
    type: str
    description: Store id when the evidence came from a Truth projection.
    required: false
  meta:
    type: dict
    description: Additional locator and evidence metadata. Producer identity is authoritative and cannot be overridden.
    required: false
  producer_call_id:
    type: str
    description: Optional durable model call identifier.
    required: false
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- truth
- evidence
- capture
- provenance
aliases:
- capture truth evidence
- add source receipt
- record evidence
- ingest claim source
- save verification source
parents:
- truth
---
