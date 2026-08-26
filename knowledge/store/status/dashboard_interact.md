---
name: Dashboard Interact
kind: capability
description: "Frozen compatibility bridge for registered root-dashboard forms. Jobs authoring is not supported: every jobs-add-job action returns form_migrated with /app/jobs. New React forms use widget-native assisted drafts with human-only submission."
capability_name: dashboard_interact
category: status
op: op.wb.dashboard_interact
schema_version: wb-capability/v1
parameters:
  action:
    type: str
    description: 'One of: form_field_set, form_open, form_submit, form_get_state.'
    required: true
  form_id:
    type: str
    description: Registered legacy form to address. jobs-add-job is migrated and rejects every action.
    required: true
  field:
    type: str
    description: Field name (form_field_set only).
    required: false
  value:
    type: any
    description: Field value (form_field_set only). Type-checked against the field's declared type in the schema.
    required: false
  timeout_seconds:
    type: float
    description: Rendezvous timeout for form_submit / form_get_state in seconds. Default 10. Ignored for other actions.
    required: false
mutates_state: true
retry_policy: manual
tags:
- status
- dashboard
- interact
aliases:
- fill form
- click submit
- drive ui
- agent ui interaction
- form bridge
- set form field
parents:
- status
---

This capability is retained compatibility infrastructure, not the extension path for new forms. `jobs-add-job` returns `{ok: false, code: "form_migrated", href: "/app/jobs"}` for every action without editing fields, opening a conversation, or submitting a job.

Use `/app/jobs` for Jobs authoring. React forms share host-owned draft assistance and leave final submission to the user; see `services/dashboard/react/assisted-drafts`. The retained bridge protocol is documented at `services/dashboard/form-bridge`.
