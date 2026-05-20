---
name: Entity List
kind: capability
description: 'List entities ordered by most-recently-updated. Optional hierarchical tag filter: tag=''person'' returns ''person'', ''person/family'', ''person/colleague'', etc.'
capability_name: entity_list
category: entities
parameters:
  tag:
    type: str
    description: Hierarchical tag filter. Prefix-and-slash match.
    required: false
  limit:
    type: int
    description: Cap on the result set; omit for everything.
    required: false
op: op.wb.entity_list
schema_version: wb-capability/v1
tags:
- entities
- entity
- list
- browse
aliases:
- list entities
- browse entities
- who do I know
- all entities
- show entities
- what is in my registry
parents:
- entities
---
