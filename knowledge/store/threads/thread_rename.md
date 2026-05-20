---
name: Thread Rename
kind: capability
description: Rewrite a thread's title (and description) — used by the action-chip 'Rename' affordance and by the LLM cluster-refinement step when it overrides an algorithmic cluster label.
capability_name: thread_rename
category: threads
op: op.wb.thread_rename
schema_version: wb-capability/v1
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
