---
name: Docs Get
kind: capability
description: '[Legacy] Get a knowledge unit by name. Use agent_docs instead.'
capability_name: docs_get
category: context
op: op.wb.docs_get
schema_version: wb-capability/v1
parameters:
  name:
    type: str
    required: true
  depth:
    type: str
    required: false
invokes:
- agent_docs
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
