---
name: Co-work Truth Analysis Job Submit
kind: capability
description: Submit one immutable typed candidate set for the exact Co-work Truth-analysis run without writing to the Truth ledger.
capability_name: cowork_truth_analysis_job_submit
category: cowork
op: op.wb.cowork_truth_analysis_job_submit
schema_version: wb-capability/v1
parameters:
  run_id:
    type: str
    description: Server-issued Truth-analysis run id. It must match the gateway-injected worker session.
    required: true
  payload:
    type: dict
    description: Output conforming exactly to the schema returned by cowork_truth_analysis_job_get, including truthful source coverage and run-owned evidence references.
    required: true
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- cowork
- truth
- analysis
- job
- constrained-agent
- submit
aliases:
- submit truth analysis job
- deliver truth candidates
- stage passage claims
parents:
- cowork
---

This is the only authoritative delivery path for the exact
`<run-id>-cowork-truth-analysis` worker. Stdout, files, terminal output, and the
hosted agent's final response do not stage candidates. The server normalizes
the typed payload, validates allowed claim kinds and expression roles,
reanchors every expression to the immutable target, verifies existing-claim
IDs against the supplied bounded context, and replaces worker-reported source
coverage with coverage derived from durable search and fetch receipts.

Evidence references are source-specific. A recorded Truth span must name an
admitted span receipt. A web item must name a completed run-owned `fetch_id`,
pass its captured-text digest, and contain an exact uniquely anchored quote. A
passage citation remains a non-attachable citation cue. Search hit IDs and
snippets cannot be submitted as evidence.

An identical normalized payload replay returns the same completed run. A
different second output is rejected. Successful submission writes only
durable operational analysis output and prepared candidates: it cannot create
or confirm a claim, connect an expression, attach evidence, edit the document,
or mint a human decision. Those consequences require a later explicit human
action through the ordinary Truth surface.

The response is deliberately compact: `ok`, schema
`wb.cowork.truth-analysis-submit-receipt/v1`, `analysis_run_id`, public
`status`, and `output_sha256`. It does not echo candidate content or imply that
ledger consequences have occurred.

Provenance keeps preparation and acceptance distinct. The staged output is
AI-prepared and bound to the run, provider/model authorization, and output
hash. If a person later chooses **Add as proposed** or connects an existing
claim, the claim/expression consequence is authored by that human actor while
retaining analysis-run and candidate metadata. A selected web source retains
its `agent_run` acquisition actor and external-quarantined provenance; the
human authors the later support decision rather than laundering the source's
origin.
