---
name: Artifact Cleanup
kind: capability
description: Run TTL-based cleanup over registered artifacts. By default, sweeps every registered artifact (filesystem, llm-cache, messages, notifications, llm-queue, …). Pass `name` to scope the sweep to a single artifact. Use `dry_run=true` (default) to preview what would be deleted. The `name` field is deliberately distinct from `artifact_save`'s `type` field (which means filesystem subtype) — they live in different namespaces.
capability_name: artifact_cleanup
category: artifacts
parameters:
  dry_run:
    type: bool
    description: 'Preview only, don''t delete (default: true)'
    required: false
  name:
    type: str
    description: Registered artifact name (e.g. 'llm-cache', 'messages'). Omit to sweep all.
    required: false
mutates_state: true
retry_policy: manual
tags:
- artifacts
- artifact
- cleanup
aliases:
- cleanup artifacts
- sweep expired
- artifact gc
- prune artifacts
- data cleanup
parents:
- artifacts
- artifacts
---
