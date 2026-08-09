---
name: Co-work Truth Analysis Search
kind: capability
description: Run one replay-safe bounded web query for the exact Co-work Truth-analysis worker and persist up to five lead-only hits.
capability_name: cowork_truth_analysis_search
category: cowork
op: op.wb.cowork_truth_analysis_search
schema_version: wb-capability/v1
parameters:
  run_id:
    type: str
    description: Server-issued Truth-analysis run id. It must match the gateway-injected worker session.
    required: true
  query:
    type: str
    description: One non-empty web query of at most 500 characters; whitespace-equivalent replay resolves to the original durable search receipt.
    required: true
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- cowork
- truth
- analysis
- websearch
- constrained-agent
aliases:
- search for truth evidence
- search truth analysis sources
- bounded claim search
parents:
- cowork
---

Only the exact `<run-id>-cowork-truth-analysis` transport session can call this
capability. The run must still be active. Each run may admit at most three
distinct normalized queries, with at most five hits per query; the worker
cannot raise either limit or choose a different search transport.

The account-model worker's $2.00 hard session ceiling does not cover this web
operation. Search uses a separately configured provider and currently has no
enforced monetary ceiling; the three-query and five-hit limits bound activity,
not provider charges. User-facing disclosure must describe that distinction
before launch.

Persisted hits contain stable opaque IDs, source titles and URLs, provider
metadata, and snippets marked as leads. Search snippets and provider-inline
page text are never supporting evidence. A later fetch must name one of these
server-admitted hit IDs; this capability never creates an evidence receipt,
claim-support relationship, ledger claim, or fact.

An identical normalized-query replay returns the original durable search and
hit identities without another outbound call. The broker writes intent before
egress and result before receipt projection. If a process stops while the
outbound outcome is uncertain, the stale operation becomes durable
`research_outcome_unknown` and is not automatically replayed. Search therefore
uses manual retry policy even though completed exact replays are idempotent.
