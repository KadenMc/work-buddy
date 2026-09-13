---
name: Entity Delete
kind: skill
description: Hard-delete an entity, cascading through tags, aliases, and references. Consent-gated (both user and agent authors must approve).
category: entities
parameters:
  entity_id:
    type: int
    description: Stable surrogate id of the entity to delete.
    required: true
  author:
    type: str
    description: '''user'' (default) or ''agent''.'
    required: false
mutates_state: true
retry_policy: manual
consent_required: true
op: op.wb.entity_delete
schema_version: wb-skill/v1
skill_name: entity_delete
tags:
- entities
- entity
- delete
- destructive
aliases:
- delete entity
- remove entity
- unregister entity
- drop entity
parents:
- entities
---
