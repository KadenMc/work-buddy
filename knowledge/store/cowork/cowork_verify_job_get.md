---
name: Co-work Verify Job Get
kind: capability
description: Read the exact immutable context and output schema bound to one job-scoped Co-work Verify worker.
capability_name: cowork_verify_job_get
category: cowork
op: op.wb.cowork_verify_job_get
schema_version: wb-capability/v1
parameters:
  job_id:
    type: str
    description: Server-issued job id. The gateway-injected worker session must encode this same id and role.
    required: true
mutates_state: false
retry_policy: manual
auto_retry: false
tags:
- cowork
- verify
- job
- constrained-agent
aliases:
- get verify job
- read verification assignment
- get coordinator context
parents:
- cowork
---

This capability is available only to a server-authored
`<job-id>-cowork-verify-<role>` session. The transport identity, not the
argument, grants access. A mismatch fails before store or document resolution.

The response is minimized by role. A `specialist` receives the immutable action
identity, exact frozen target, one criterion/check/binding assignment, and its
typed output schema; it does not receive the complete document, user purpose,
prior decisions, candidates, or allowed change ranges.

Coordinators and revisers receive their permitted frozen document context,
purpose and preservation boundary, exact criteria/checks, normalized results,
relevant prior dispositions and human review outcomes, candidate state where
applicable, policy boundaries, and the role-specific output schema. A
post-revision coordinator also receives the server-recomputed affected-region
candidate evaluation, which must match the portable proof bound into its
authorization. Document and criterion content are untrusted job data and
cannot name tools or broaden authority.
