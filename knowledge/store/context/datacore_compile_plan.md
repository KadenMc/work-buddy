---
name: Datacore Compile Plan
kind: capability
description: 'Compile a structured JSON query plan into a Datacore query string. Plan keys: target (required), path, tags, tags_any, status, text_contains, exists, frontmatter, child_of, parent, expressions, negate.'
capability_name: datacore_compile_plan
category: context
op: op.wb.datacore_compile_plan
schema_version: wb-capability/v1
parameters:
  plan_json:
    type: str
    description: JSON string of the query plan
    required: true
tags:
- context
- datacore
- compile
- plan
aliases:
- compile query plan
- plan to query
- structured query
- build datacore query
parents:
- context
requires:
- obsidian
- datacore
---
