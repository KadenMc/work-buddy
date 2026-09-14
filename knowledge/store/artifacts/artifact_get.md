---
name: Artifact Get
kind: skill
description: Retrieve an artifact by ID (filename stem). Returns metadata and content (inline if < 50KB, otherwise file path).
category: artifacts
op: op.wb.artifact_get
schema_version: wb-skill/v1
parameters:
  id:
    type: str
    description: Artifact ID (filename stem, e.g. '20260412-093000_weekly-review')
    required: true
skill_name: artifact_get
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
---
