---
name: Datacore Compile Plan
kind: skill
description: 'Compile a structured JSON query plan into a Datacore query string. Plan keys: target (required), path, tags, tags_any, status, text_contains, exists, frontmatter, child_of, parent, expressions, negate.'
category: context
op: op.wb.datacore_compile_plan
schema_version: wb-skill/v1
parameters:
  plan_json:
    type: str
    description: JSON string of the query plan
    required: true
skill_name: datacore_compile_plan
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
