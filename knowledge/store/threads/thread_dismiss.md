---
name: Thread Dismiss
kind: capability
description: Mark a thread as dismissed via the standard FSM transition. For group sub-threads this is the 'do nothing with this cluster' action. For umbrellas it cascades through the existing dismiss flow.
capability_name: thread_dismiss
category: threads
op: op.wb.thread_dismiss
schema_version: wb-capability/v1
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
