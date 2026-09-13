---
name: Context Wellness
kind: skill
description: Explicit legacy-only wellness summary from Obsidian journal files. Unavailable without an explicit Obsidian opt-in; native Journal profiles own current markers.
category: context
op: op.wb.context_wellness
schema_version: wb-skill/v1
parameters:
  days:
    type: int
    description: Days of wellness data (default 14)
    required: false
skill_name: context_wellness
tags:
- context
- wellness
aliases:
- wellness
- health tracking
- self-care data
parents:
- context
requires:
- obsidian
---
