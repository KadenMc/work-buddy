---
name: Entity Update
kind: capability
description: Update an entity's canonical name and/or description. Tags + aliases are managed through their own capabilities so a rename PATCH can't accidentally wipe them.
capability_name: entity_update
category: entities
parameters:
  entity_id:
    type: int
    description: Stable surrogate id (NOT the canonical name).
    required: true
  canonical_name:
    type: str
    description: New display name. Re-normalizes; rejects on collision with another entity.
    required: false
  description:
    type: str
    description: New description. Pass empty string to clear; omit to leave unchanged.
    required: false
  author:
    type: str
    description: '''user'' (default) or ''agent''.'
    required: false
  source_path:
    type: str
    description: Document/session/agent path; with source_kind, records an update reference.
    required: false
  source_kind:
    type: str
    description: One of document, chat, task, agent, manual.
    required: false
mutates_state: true
retry_policy: manual
op: op.wb.entity_update
schema_version: wb-capability/v1
tags:
- entities
- entity
- update
aliases:
- update entity
- rename entity
- edit entity
- change entity description
parents:
- entities
---
