---
name: Datacore Validate
kind: capability
description: Validate a Datacore query string without executing it. Returns parse error details if invalid.
capability_name: datacore_validate
category: context
parameters:
  query:
    type: str
    description: Datacore query string to validate
    required: true
tags:
- context
- datacore
- validate
aliases:
- validate query
- check query syntax
- lint datacore query
- verify query parses
- parse check
- query validator
parents:
- context
- context
requires:
- obsidian
- datacore
---
