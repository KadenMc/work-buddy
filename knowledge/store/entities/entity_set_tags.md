---
name: Entity Set Tags
kind: capability
description: Replace the full tag set on an entity. Pass an empty list to clear. Tags are normalized; exact duplicates and redundant ancestor tags (person when person/family is present) are collapsed before writing.
capability_name: entity_set_tags
category: entities
parameters:
  entity_id:
    type: int
    description: Stable surrogate id.
    required: true
  tags:
    type: list
    description: Replacement tag list. Hierarchical, multi-valued.
    required: true
  author:
    type: str
    description: '''user'' (default) or ''agent''.'
    required: false
mutates_state: true
retry_policy: manual
op: op.wb.entity_set_tags
schema_version: wb-capability/v1
tags:
- entities
- entity
- set
- tags
aliases:
- set tags
- replace tags
- update tags
- retag entity
- classify entity
parents:
- entities
---
