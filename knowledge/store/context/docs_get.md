---
name: Docs Get
kind: skill
description: '[Legacy] Get a knowledge unit by name. Use agent_docs instead.'
category: context
op: op.wb.docs_get
schema_version: wb-skill/v1
parameters:
  name:
    type: str
    required: true
  depth:
    type: str
    required: false
invokes:
- agent_docs
skill_name: docs_get
tags:
- context
- docs
- get
aliases:
- legacy knowledge get
- old docs get
- legacy unit lookup
- deprecated docs fetch
parents:
- context
---
