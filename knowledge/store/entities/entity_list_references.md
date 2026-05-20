---
name: Entity List References
kind: capability
description: List references for an entity, newest first. Default limit 50 to keep dashboard responses small.
capability_name: entity_list_references
category: entities
parameters:
  entity_id:
    type: int
    description: Stable surrogate id.
    required: true
  limit:
    type: int
    description: Cap on the result set. Default 50; pass a larger value for the full history.
    required: false
op: op.wb.entity_list_references
schema_version: wb-capability/v1
tags:
- entities
- entity
- list
- references
aliases:
- list references
- entity history
- where mentioned
- mention history
- reference log
parents:
- entities
---
