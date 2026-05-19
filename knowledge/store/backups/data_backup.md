---
name: Data Backup
kind: capability
description: Take a snapshot of work-buddy's vital SQLite DBs (task_metadata, projects, messages, threads). Hot-backup, tar+gzip, write manifest, optionally push to GitHub Releases. Called by the hourly sidecar cron AND by the user via /wb-backup-now.
capability_name: data_backup
category: backups
op: op.wb.data_backup
schema_version: wb-capability/v1
parameters:
  manual:
    type: bool
    description: Tag the snapshot as user-triggered (suffix '-manual'; separate retention bucket; default False = cron-driven.
    required: false
  push_remote:
    type: bool
    description: Override auto-detection of remote push. None (default) means push iff backups.github.repo is configured.
    required: false
mutates_state: true
retry_policy: manual
tags:
- backups
- data
- backup
aliases:
- backup work-buddy data
- snapshot vital DBs
- push backup to github
parents:
- backups
---
