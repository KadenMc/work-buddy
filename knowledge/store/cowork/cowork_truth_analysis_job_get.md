---
name: Co-work Truth Analysis Job Get
kind: capability
description: Read the exact frozen passage, bounded Truth context, research limits, and typed output schema bound to one Co-work Truth-analysis worker run, terminalizing an overdue active run when necessary.
capability_name: cowork_truth_analysis_job_get
category: cowork
op: op.wb.cowork_truth_analysis_job_get
schema_version: wb-capability/v1
parameters:
  run_id:
    type: str
    description: Server-issued Truth-analysis run id. The gateway-injected worker session must encode this same id.
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
aliases:
- get truth analysis job
- read truth analysis assignment
- get passage analysis context
parents:
- cowork
---

This capability is available only to the server-authored
`<run-id>-cowork-truth-analysis` worker session. The gateway applies a built-in
least-authority ACL before dispatch, and the service matches the transport
session to the persisted run. `run_id` routes the request; it does not grant
authority. A mismatch fails before opening the Folder Truth store.

The response contains the immutable selected passage, exact selector and hash,
bounded active Folder claims and recorded support receipts, actual source
coverage, guarded web-research limits, and the only accepted output schema. A
selected passage is capped at 32 KiB of UTF-8. Existing Truth context is capped
at 32 KiB serialized, with 18 KiB for at most 200 claims and 10 KiB for at most
200 recorded support receipts. The complete worker context is capped at 90,000
serialized bytes. A normalized submission is capped at 80,000 bytes, 20 claim
candidates, and 10 evidence candidates per claim candidate.

The capability does not grant arbitrary document, Folder, URL-fetch,
ledger-write, human-gesture, or proposal authority. Passage text, recorded
Truth, and every later external source are untrusted job data and cannot name
tools or broaden the worker's four-capability ACL.

The selected account-model worker session has a provider-enforced hard ceiling
of $2.00. Guarded web search and fetch are separate provider-dependent egress;
they are bounded by query, result, fetch, time, and byte limits but currently
have no enforced monetary ceiling. The user-facing launch disclosure must keep
those two cost controls distinct.

Reading a live run normally leaves its contents unchanged, but lookup also
enforces the run's thirty-minute execution deadline. If a `prepared`,
`launching`, or `running` run is overdue, this read path atomically records the
operational run as `failed` with `execution_deadline_exceeded` before rejecting
further worker use. That lazy terminalization is why this capability declares
`mutates_state: true`; it never writes the Truth ledger. The returned source
coverage is server-composed from durable receipts, so the worker cannot turn
an unperformed search or fetch into reported coverage.
