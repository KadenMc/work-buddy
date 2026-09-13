---
name: Artifact Delete
kind: skill
description: Delete an artifact and its metadata by ID.
category: artifacts
op: op.wb.artifact_delete
schema_version: wb-skill/v1
parameters:
  id:
    type: str
    description: Artifact ID to delete
    required: true
mutates_state: true
retry_policy: manual
skill_name: artifact_delete
tags:
- artifacts
- artifact
- delete
aliases:
- delete artifact
- remove artifact
- drop artifact
- erase artifact
- remove saved output
- clean up artifact
- delete report file
parents:
- artifacts
---
