---
name: Thread Dismiss
kind: skill
description: Mark a thread as dismissed via the standard FSM transition. For group sub-threads this is the 'do nothing with this cluster' action. For umbrellas it cascades through the existing dismiss flow.
category: threads
op: op.wb.thread_dismiss
schema_version: wb-skill/v1
parameters:
  thread_id:
    type: str
    description: Thread to dismiss
    required: true
  reason:
    type: str
    description: Optional free-text reason recorded on the dismiss event
    required: false
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
skill_name: thread_dismiss
tags:
- threads
- thread
- dismiss
aliases:
- dismiss thread
- skip thread
- ignore thread
- drop thread
parents:
- threads
---
