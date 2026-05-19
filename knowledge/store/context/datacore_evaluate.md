---
name: Datacore Evaluate
kind: capability
description: Evaluate a Datacore expression (e.g. arithmetic, field access).
capability_name: datacore_evaluate
category: context
op: op.wb.datacore_evaluate
schema_version: wb-capability/v1
parameters:
  expression:
    type: str
    description: Datacore expression
    required: true
  source_path:
    type: str
    description: Vault path for 'this' context
    required: false
tags:
- context
- datacore
- evaluate
aliases:
- datacore eval
- evaluate expression
- compute datacore expression
- datacore calculation
- eval vault expression
- datacore formula
parents:
- context
requires:
- obsidian
- datacore
---
