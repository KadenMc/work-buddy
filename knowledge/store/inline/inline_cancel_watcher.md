---
name: Inline Cancel Watcher
kind: capability
description: Cancel a single persistent watcher by ID.
capability_name: inline_cancel_watcher
category: inline
op: op.wb.inline_cancel_watcher
schema_version: wb-capability/v1
parameters:
  watcher_id:
    type: string
    description: Watcher identifier
    required: true
mutates_state: true
retry_policy: manual
tags:
- inline
- cancel
- watcher
aliases:
- cancel watcher
- delete watcher
parents:
- inline
---
