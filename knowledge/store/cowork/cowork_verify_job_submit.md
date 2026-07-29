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

This is the only authoritative delivery path for enabled job-scoped
specialists, revisers, coordinators, and historical Co-think workers.
Deterministic checks run in the domain process; admitted model-backed checks use
one narrow specialist submission per frozen assignment. Stdout, files,
terminal output, and the hosted agent's final response have no effect.

An identical retry is idempotent; a different second payload is rejected.
Specialist results are schema-validated, reanchored to exact frozen-target
evidence, and recorded as typed check executions/results. The next specialist
or initial coordinator starts only after those consequences commit. Specialist
output cannot publish to Review or request revision. Reviser output remains a
private candidate. Only a coordinator submission can append routing
dispositions and ask the server to create an ordinary immutable proposal; a
human still decides and applies that proposal through the existing sitting.

The server first validates and durably records the normalized typed payload and
its hash as `submitted`, then atomically leases deterministic consequence
projection. If the process stops at that boundary, sidecar reconciliation
resumes projection from the stored payload without another model call.
Concurrent projectors converge, and a correction route is rejected unless the
post-revision candidate has a passing deterministic affected-region proof.
