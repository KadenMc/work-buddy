---
name: Docs Get
kind: capability
description: '[Legacy] Get a knowledge unit by name. Use agent_docs instead.'
capability_name: docs_get
category: context
parameters:
  name:
    type: str
    required: true
  depth:
    type: str
    required: false
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
- context
---
