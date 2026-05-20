---
name: Entity Remove Alias
kind: capability
description: Detach an alias from an entity. No-op if not attached.
capability_name: entity_remove_alias
category: entities
parameters:
  entity_id:
    type: int
    description: Stable surrogate id.
    required: true
  alias:
    type: str
    description: Alias to detach. Matched case-insensitively.
    required: true
  author:
    type: str
    description: '''user'' (default) or ''agent''.'
    required: false
mutates_state: true
retry_policy: manual
op: op.wb.entity_remove_alias
schema_version: wb-capability/v1
tags:
- entities
- entity
- remove
- alias
aliases:
- remove alias
- detach alias
- drop alias
- unalias entity
parents:
- entities
---
