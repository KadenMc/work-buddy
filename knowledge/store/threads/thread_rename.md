---
name: Thread Rename
kind: skill
description: Rewrite a thread's title (and description) — used by the action-chip 'Rename' affordance and by the LLM cluster-refinement step when it overrides an algorithmic cluster label.
category: threads
op: op.wb.thread_rename
schema_version: wb-skill/v1
parameters:
  thread_id:
    type: str
    description: Thread to rename
    required: true
  new_title:
    type: str
    description: New title (also used as description)
    required: true
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
skill_name: thread_rename
tags:
- threads
- thread
- rename
aliases:
- rename thread
- change thread title
- edit cluster label
parents:
- threads
---
