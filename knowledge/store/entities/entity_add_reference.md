---
name: Entity Add Reference
kind: capability
description: Explicitly append a reference row for an entity. The standard recording path is the side-effect of entity_resolve/create/update; this exists for scripts and dashboard-driven recording.
capability_name: entity_add_reference
category: entities
parameters:
  entity_id:
    type: int
    description: Stable surrogate id.
    required: true
  source_path:
    type: str
    description: Document/session/agent path being referenced.
    required: true
  source_kind:
    type: str
    description: One of document, chat, task, agent, manual.
    required: true
  snippet:
    type: str
    description: Optional excerpt or context note for this reference.
    required: false
mutates_state: true
retry_policy: manual
op: op.wb.entity_add_reference
schema_version: wb-capability/v1
tags:
- entities
- entity
- add
- reference
- append-only
aliases:
- add reference
- record mention
- note reference
- append entity reference
parents:
- entities
---
