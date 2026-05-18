---
name: Artifact Save
kind: capability
description: Save an artifact (context bundle, export, report, snapshot, or scratch) to the centralized data store with metadata and TTL-based lifecycle.
capability_name: artifact_save
category: artifacts
parameters:
  content:
    type: str
    description: Content to save (text)
    required: true
  type:
    type: str
    description: 'Artifact type: context (7d TTL), export (90d), report (30d), snapshot (14d), scratch (3d)'
    required: true
  slug:
    type: str
    description: Short descriptive name (kebab-case, used in filename)
    required: true
  ext:
    type: str
    description: 'File extension (default: json)'
    required: false
  tags:
    type: str
    description: Comma-separated tags for filtering
    required: false
  description:
    type: str
    description: Human-readable description
    required: false
  ttl_days:
    type: int
    description: Override default TTL in days
    required: false
  agent_session_id:
    type: str
    description: Session ID (auto-injected by gateway)
    required: false
mutates_state: true
retry_policy: replay
tags:
- artifacts
- artifact
- save
aliases:
- save artifact
- store output
- write artifact
- save bundle
- save export
- save report
parents:
- artifacts
- artifacts
---
