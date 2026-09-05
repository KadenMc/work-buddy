---
name: Journal Smart Processing Complete
kind: capability
description: Commit one lease-bound Journal Smart worker classification as the capture's routing, annotation, and optional task proposal.
capability_name: journal_smart_processing_complete
category: journal
op: op.wb.journal_smart_processing_complete
schema_version: wb-capability/v1
parameters:
  request_id:
    type: str
    description: Exact Smart processing request ID from the scoped worker brief.
    required: true
  lease_token:
    type: str
    description: Exact one-time lease capability from the scoped worker brief.
    required: true
  target:
    type: str
    description: Chosen destination for the saved capture, either log or running_notes.
    required: true
  summary:
    type: str
    description: Classification summary under 240 characters. It never rewrites the saved capture.
    required: true
  effects:
    type: list[str]
    description: At most three short factual phrases, each under 160 characters.
    required: true
  follow_up:
    type: dict
    description: Optional single task_proposal object with task_text and rationale. Omit it when the capture proposes no task.
    required: false
mutates_state: true
retry_policy: verify_first
tags:
- journal
- internal
- smart
parents:
- journal
requires: []
---

The output call validates the structured result against the only shape a Smart
worker may commit, causally acknowledges the worker's recorded input
disclosure, and binds the routing, annotation, and optional proposal to the
capture in one durable step. `auto` is not a destination, a proposal only
enters human review and never creates a task, and no other action kind, URL,
or identifier is accepted. It never accepts a caller-selected author or worker
session.
