---
name: Memory Write
kind: skill
description: Store a personal fact, preference, or constraint in memory
category: memory
op: op.wb.memory_write
schema_version: wb-skill/v1
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
skill_name: memory_write
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
