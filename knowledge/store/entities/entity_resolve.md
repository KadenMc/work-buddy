---
name: Entity Resolve
kind: capability
description: Federated lookup across the entity store + the project registry. Returns all matches in parallel, flagged by provider. Optionally records a reference when source_path + source_kind are supplied.
capability_name: entity_resolve
category: entities
parameters:
  query:
    type: str
    description: A name, alias, or slug to resolve. Case-insensitive.
    required: true
  source_path:
    type: str
    description: Document/session/agent path. When set with source_kind, each entities-provider match is recorded as a reference (de-dup window applies).
    required: false
  source_kind:
    type: str
    description: One of document, chat, task, agent, manual. Required alongside source_path for side-effect reference recording.
    required: false
op: op.wb.entity_resolve
schema_version: wb-capability/v1
tags:
- entities
- entity
- resolve
- federated
- lookup
aliases:
- resolve entity
- who is
- what is
- lookup name
- federated resolve
- find by name
- name resolution
parents:
- entities
---
