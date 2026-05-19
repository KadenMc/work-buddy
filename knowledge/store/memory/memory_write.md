---
name: Memory Write
kind: capability
description: Store a personal fact, preference, or constraint in memory
capability_name: memory_write
category: memory
op: op.wb.memory_write
schema_version: wb-capability/v1
parameters:
  content:
    type: str
    description: The fact or preference to remember
    required: true
  kind:
    type: str
    description: 'Memory kind: preference, habit, constraint, blindspot, relationship, decision, life-context (default preference)'
    required: false
  domain:
    type: str
    description: 'Domain: work, life, health (default life)'
    required: false
tags:
- memory
- write
aliases:
- remember this
- save to memory
- store a preference
- add memory
- record fact
- save to hindsight
- memorize this
parents:
- memory
requires:
- hindsight
---
