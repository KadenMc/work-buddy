---
name: Data Backup List
kind: capability
description: List local snapshots (and optionally remote ones). Each entry includes snapshot_id, timestamp, size, manual flag, and the manifest summary (commit + schema versions).
capability_name: data_backup_list
category: backups
op: op.wb.data_backup_list
schema_version: wb-capability/v1
parameters:
  include_remote:
    type: bool
    description: Also list snapshots on the configured GitHub Releases repo. Default False (local-only).
    required: false
tags:
- backups
- data
- backup
- list
aliases:
- list backups
- available snapshots
- what backups exist
parents:
- backups
---
