---
name: Docs Move
kind: skill
description: Move a unit to a new path. Updates parent/child references; for a Workflow, preserves workflow_id and rewrites path-based Directions bindings.
category: context
op: op.wb.docs_move
schema_version: wb-skill/v1
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
skill_name: docs_move
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
---
