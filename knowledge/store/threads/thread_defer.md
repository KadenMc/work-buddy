---
name: Thread Defer
kind: capability
description: Defer a thread so it resurfaces at a future time. Sets the cached resurface_at field; the existing Later mechanic re-surfaces the thread when the time arrives.
capability_name: thread_defer
category: threads
op: op.wb.thread_defer
schema_version: wb-capability/v1
parameters:
  thread_id:
    type: str
    description: Thread to defer
    required: true
  duration_hours:
    type: float
    description: Hours to wait before resurfacing (default 24)
    required: false
  resurface_at:
    type: str
    description: Absolute ISO timestamp to resurface at (overrides duration_hours)
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
- defer
aliases:
- snooze thread
- later
- remind me later
- defer thread
parents:
- threads
---
