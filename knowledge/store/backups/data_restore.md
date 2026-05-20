---
name: Data Restore
kind: capability
description: Restore work-buddy's vital SQLite DBs from a snapshot. Validates the manifest (refuses if the snapshot's commit or schema is newer than the running code), unpacks to staging, runs migrations forward, verifies integrity, then atomically swaps into place (the old DBs are moved to .data/db.pre_restore_<ts>/ for safety).
capability_name: data_restore
category: backups
op: op.wb.data_restore
schema_version: wb-capability/v1
parameters:
  snapshot_id:
    type: str
    description: Snapshot ID (e.g. 'snap-2026-05-11T14-23-00Z') or, if from_remote=True, a GitHub release tag.
    required: true
  from_remote:
    type: bool
    description: If True, download the tarball from GitHub Releases first (via `gh release download`). Default False (local-only).
    required: false
  force:
    type: bool
    description: Override safety checks (newer-schema, newer-commit). Use sparingly — these checks exist to prevent silent corruption.
    required: false
mutates_state: true
retry_policy: manual
tags:
- backups
- data
- restore
aliases:
- restore work-buddy data
- restore from snapshot
- recover from backup
- time travel
parents:
- backups
---
