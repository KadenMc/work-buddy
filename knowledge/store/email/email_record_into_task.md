---
name: Email Record Into Task
kind: capability
description: File an email cluster as a context section on an existing task's linked note. Use when the cluster is context for ongoing work (replies on an active deliverable, PR-review notifications about a task you're already tracking) rather than a new task. The target task must already have a note attached; this capability does not implicitly create one. Appends a bulleted 'Emails recorded' section listing each email's subject + sender + date.
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
    description: Task ID (e.g. 't-a3f8c1e2') to file the cluster against. Must already have a linked note.
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
requires:
- obsidian
---
