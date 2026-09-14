---
name: Entity Add Reference
kind: skill
description: Explicitly append a reference row for an entity. The standard recording path is the side-effect of entity_resolve/create/update; this exists for scripts and dashboard-driven recording.
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
schema_version: wb-skill/v1
skill_name: entity_add_reference
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
