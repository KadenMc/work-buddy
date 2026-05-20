---
name: Entity Add Alias
kind: capability
description: Attach an alias to an entity. Globally unique (one alias, one entity); raises on collision.
capability_name: entity_add_alias
category: entities
parameters:
  entity_id:
    type: int
    description: Stable surrogate id.
    required: true
  alias:
    type: str
    description: Alternative name. Display casing preserved; lookup is case-insensitive.
    required: true
  author:
    type: str
    description: '''user'' (default) or ''agent''.'
    required: false
mutates_state: true
retry_policy: manual
op: op.wb.entity_add_alias
schema_version: wb-capability/v1
tags:
- entities
- entity
- add
- alias
aliases:
- add alias
- alias entity
- attach alternative name
- alternate name
parents:
- entities
---
