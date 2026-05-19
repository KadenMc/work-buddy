---
name: Chrome Tab Move
kind: capability
description: Move Chrome tabs to a specific position or window.
capability_name: chrome_tab_move
category: context
op: op.wb.chrome_tab_move
schema_version: wb-capability/v1
parameters:
  tab_ids:
    type: list
    description: List of Chrome tab IDs to move
    required: true
  index:
    type: int
    description: Position index (-1 = end of window)
    required: false
  window_id:
    type: int
    description: Target window ID (omit for current window)
    required: false
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
tags:
- context
- chrome
- tab
- move
aliases:
- move tab
- reorder tabs
- rearrange tabs
- shift chrome tabs
- send tab to another window
- reposition tab
parents:
- context
requires:
- chrome_extension
---
