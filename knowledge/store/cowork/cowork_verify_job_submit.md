---
name: Co-work Verify Job Submit
kind: capability
description: Submit one typed output for the exact job and role encoded by a constrained Co-work Verify worker session.
capability_name: cowork_verify_job_submit
category: cowork
op: op.wb.cowork_verify_job_submit
schema_version: wb-capability/v1
parameters:
  job_id:
    type: str
    description: Server-issued job id. It must match the gateway-injected worker session.
    required: true
  payload:
    type: dict
    description: Output conforming exactly to the role-specific schema returned by cowork_verify_job_get.
    required: true
mutates_state: true
retry_policy: manual
auto_retry: false
tags:
- cowork
- verify
- job
- constrained-agent
- submit
aliases:
- submit verify job
- deliver coordinator decision
- deliver revision candidate
- submit co-think perspective
parents:
- cowork
---

This is the only authoritative delivery path for enabled job-scoped reviser,
coordinator, and Co-think workers. The specialist role is reserved by the
transport contract, but model-based specialist submission is not enabled; the
current deterministic exact-term check runs in the domain process. Stdout,
files, terminal output, and the hosted agent's final response have no effect.

An identical retry is idempotent; a different second payload is rejected.
Specialist output cannot publish to Review. Reviser output remains a private
candidate. Only a coordinator submission can append routing dispositions and
ask the server to create an ordinary immutable proposal; a human still decides
and applies that proposal through the existing sitting.

The server first validates and durably records the normalized typed payload and
its hash as `submitted`, then atomically leases deterministic consequence
projection. If the process stops at that boundary, sidecar reconciliation
resumes projection from the stored payload without another model call.
Concurrent projectors converge, and a correction route is rejected unless the
post-revision candidate has a passing deterministic affected-region proof.
