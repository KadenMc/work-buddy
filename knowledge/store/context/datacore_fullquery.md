---
name: Datacore Fullquery
kind: capability
description: Execute a Datacore query with timing and revision metadata. Same as datacore_query but includes duration_s and revision.
capability_name: datacore_fullquery
category: context
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
- context
requires:
- obsidian
- datacore
---
