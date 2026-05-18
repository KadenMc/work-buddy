---
name: Dashboard Interact
kind: capability
description: Drive a dashboard form on the user's behalf — fill fields, open the form, click submit, or read current state. Single typed entry point for chat-walkthrough agents; each call is validated against the form's registered FormSchema before anything reaches the frontend. See the brief's structural section for the form_id and field names you can address.
capability_name: dashboard_interact
category: status
parameters:
  action:
    type: str
    description: 'One of: form_field_set, form_open, form_submit, form_get_state.'
    required: true
  form_id:
    type: str
    description: Registered form to address (e.g. 'jobs-add-job').
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
- status
---
