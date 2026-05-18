---
name: Artifact Delete
kind: capability
description: Delete an artifact and its metadata by ID.
capability_name: artifact_delete
category: artifacts
parameters:
  id:
    type: str
    description: Artifact ID to delete
    required: true
mutates_state: true
retry_policy: manual
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
- artifacts
---
