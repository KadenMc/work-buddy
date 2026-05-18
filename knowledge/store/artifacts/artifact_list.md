---
name: Artifact List
kind: capability
description: List artifacts in the data store, filtered by type, recency, tags, or session. Sorted by creation time (newest first).
capability_name: artifact_list
category: artifacts
parameters:
  type:
    type: str
    description: Filter by type (context, export, report, snapshot, scratch)
    required: false
  since:
    type: str
    description: ISO datetime — only artifacts after this time
    required: false
  tags:
    type: str
    description: Comma-separated tags — artifact must have all
    required: false
  session_id:
    type: str
    description: Filter to artifacts from this session
    required: false
  include_expired:
    type: bool
    description: 'Include expired artifacts (default: false)'
    required: false
  limit:
    type: int
    description: 'Max results (default: 50)'
    required: false
tags:
- artifacts
- artifact
- list
aliases:
- list artifacts
- show artifacts
- find artifacts
- browse data
- artifact inventory
parents:
- artifacts
- artifacts
---
