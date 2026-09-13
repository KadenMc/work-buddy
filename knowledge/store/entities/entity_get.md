---
name: Entity Get
kind: skill
description: Fetch a single entity by canonical name, alias, or integer id. Returns tags, aliases, and the 5 most-recent reference rows.
category: entities
parameters:
  name_or_id:
    type: str
    description: Canonical name, alias, or integer id (numeric string accepted).
    required: true
op: op.wb.entity_get
schema_version: wb-skill/v1
skill_name: entity_get
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
