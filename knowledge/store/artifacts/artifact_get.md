---
name: Artifact Get
kind: capability
description: Retrieve an artifact by ID (filename stem). Returns metadata and content (inline if < 50KB, otherwise file path).
capability_name: artifact_get
category: artifacts
parameters:
  id:
    type: str
    description: Artifact ID (filename stem, e.g. '20260412-093000_weekly-review')
    required: true
tags:
- artifacts
- artifact
- get
aliases:
- get artifact
- read artifact
- fetch artifact
- retrieve artifact
- open artifact
- load artifact
- artifact contents
parents:
- artifacts
- artifacts
---
