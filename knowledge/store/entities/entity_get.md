---
name: Entity Get
kind: capability
description: Fetch a single entity by canonical name, alias, or integer id. Returns tags, aliases, and the 5 most-recent reference rows.
capability_name: entity_get
category: entities
parameters:
  name_or_id:
    type: str
    description: Canonical name, alias, or integer id (numeric string accepted).
    required: true
op: op.wb.entity_get
schema_version: wb-capability/v1
tags:
- entities
- entity
- get
aliases:
- get entity
- entity details
- entity info
- fetch entity
- read entity
- show entity
parents:
- entities
---
