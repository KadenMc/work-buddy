---
name: Context Wellness
kind: capability
description: Wellness tracker summary from recent journal entries
capability_name: context_wellness
category: context
op: op.wb.context_wellness
schema_version: wb-capability/v1
parameters:
  days:
    type: int
    description: Days of wellness data (default 14)
    required: false
tags:
- context
- wellness
aliases:
- wellness
- health tracking
- sleep exercise mood
- self-care data
parents:
- context
---
