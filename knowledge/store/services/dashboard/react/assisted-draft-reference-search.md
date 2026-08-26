---
name: Assisted Draft Reference Search
kind: capability
description: "Search one form-authorized, host-visible reference projection for a hosted assisted-draft worker without dispatching any returned capability or workflow."
capability_name: assisted_draft_reference_search
category: assistance
op: op.wb.assisted_draft_reference_search
schema_version: wb-capability/v1
parameters:
  assistant_session_id:
    type: string
    description: "Exact assistant session from the worker's server-authored binding."
    required: true
  conversation_id:
    type: string
    description: Bound conversation ID.
    required: true
  consumer:
    type: string
    description: Bound assistance inbox consumer.
    required: true
  generation:
    type: string
    description: Exact live lease generation.
    required: true
  message_id:
    type: string
    description: Exact snapshot message already consumed through assisted_draft_context_get.
    required: true
  consumption_receipt_id:
    type: string
    description: Exact generation-bound context receipt for the snapshot message.
    required: true
  request_id:
    type: string
    description: Caller-stable identity for one immutable reference query.
    required: true
  reference_kind:
    type: string
    description: "Form-authorized reference projection: job_capability or job_workflow."
    required: true
  query:
    type: string
    description: Bounded name, alias, or description query for the reference catalog.
    required: true
mutates_state: true
retry_policy: manual
auto_retry: false
parents:
- services/dashboard/react/assisted-drafts
tags:
- dashboard
- assistance
- jobs
- discovery
---

Returns at most eight names, one-line descriptions, slash aliases and reduced
parameter schemas from the same canonical projection used by the Jobs picker.
Each request is bound to the exact session, active Start, generation, consumed
turn and manifest-declared reference scope. Its payload and Sources disclosure
receipt are persisted for stable replay.

This is discovery for form authoring, not execution authority. It cannot invoke
a capability, start a workflow, search the web, create or schedule a job, submit
a form, or read arbitrary registry prose. Returned names can reach the visible
draft only through the existing typed patch, conflict and Undo protocol; the
human's normal form action remains the sole submission path.
