---
name: Datacore Run Plan
kind: capability
description: Compile and execute a structured query plan in one step. Preferred over raw datacore_query when building queries programmatically — the plan schema is simpler and validates before execution.
capability_name: datacore_run_plan
category: context
parameters:
  plan_json:
    type: str
    description: JSON string of the query plan
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
- run
- plan
aliases:
- run query plan
- execute plan
- natural language vault query
- structured vault search
parents:
- context
- context
requires:
- obsidian
- datacore
---
