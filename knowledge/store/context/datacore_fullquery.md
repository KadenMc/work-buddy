---
name: Datacore Fullquery
kind: skill
description: Execute a Datacore query with timing and revision metadata. Same as datacore_query but includes duration_s and revision.
category: context
op: op.wb.datacore_fullquery
schema_version: wb-skill/v1
parameters:
  query:
    type: str
    description: Datacore query string
    required: true
  fields:
    type: str
    description: 'Comma-separated fields. Default: all.'
    required: false
  limit:
    type: int
    description: Max results (default 50)
    required: false
skill_name: datacore_fullquery
tags:
- context
- datacore
- fullquery
aliases:
- datacore fullquery
- timed vault query
- detailed datacore query
- vault query with timing
- datacore query debug
- query timing metadata
parents:
- context
requires:
- obsidian
- datacore
---
