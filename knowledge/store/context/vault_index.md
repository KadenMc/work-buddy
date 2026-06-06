---
name: Vault Index
kind: capability
description: Build or check the vault semantic index. 'status' returns per-vault chunk/vector counts, pending backlog, last build, and DB size — read locally, so it works even when the embedding service is down. 'build' incrementally (re)indexes changed files and encodes new chunks in-service (shared inference broker, BACKGROUND priority); it skips when a build is already running. The 5-minute sidecar cron calls this with action=build.
capability_name: vault_index
category: context
op: op.wb.vault_index
schema_version: wb-capability/v1
parameters:
  action:
    type: str
    description: "'build' (default) or 'status'"
    required: false
  force:
    type: bool
    description: Rebuild every reachable vault from scratch (default false). A full rebuild encodes tens of thousands of chunks — run it via `python -m work_buddy.vault_index`, not this capability.
    required: false
tags:
- context
- vault
- index
aliases:
- build vault index
- vault index status
- reindex vault
- rebuild vault index
parents:
- context
---

Surface for the vault semantic index. Pairs with `vault_search` (query).
Incremental builds are fast (mtime check); the sidecar `vault-index-rebuild` job runs
`action=build` every 5 minutes and self-skips when a build is already in progress.
