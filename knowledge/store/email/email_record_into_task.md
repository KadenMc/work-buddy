---
name: Email Record Into Task
kind: capability
description: Append an email cluster as context in an existing task's Co-work knowledge document. The target task must already have an active document.
capability_name: email_record_into_task
category: email
op: op.wb.email_record_into_task
schema_version: wb-capability/v1
parameters:
  thread_id:
    type: str
    description: Email-cluster sub-thread carrying the emails to record
    required: true
  target_task_id:
    type: str
    description: Native task ID whose active Co-work document receives the context.
    required: true
  section_heading:
    type: str
    description: 'Optional section-heading override (default: ''Emails recorded'').'
    required: false
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
tags:
- email
- record
- into
- task
aliases:
- record emails into task
- file emails as task context
- attach emails to existing task
parents:
- email
requires: []
---

The native result carries task/document identity, task revision, and mutation
receipt metadata. It never reports or writes a Markdown note path.
