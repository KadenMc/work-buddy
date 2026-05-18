---
name: Docs Move
kind: capability
description: Move a unit to a new path. Updates all parent/child references across the store.
capability_name: docs_move
category: context
parameters:
  old_path:
    type: str
    description: Current path
    required: true
  new_path:
    type: str
    description: New path
    required: true
mutates_state: true
retry_policy: manual
tags:
- context
- docs
- move
aliases:
- move unit
- rename knowledge
- repath
- rename docs path
- relocate knowledge
- change unit path
- move documentation
parents:
- context
- context
---
