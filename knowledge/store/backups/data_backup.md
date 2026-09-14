---
name: Data Backup
kind: skill
description: Take a local snapshot of work-buddy's vital SQLite DBs and portable Truth exports. Remote GitHub upload requires an explicit persistent private-content opt-in or exact one-shot high consent.
category: backups
op: op.wb.data_backup
schema_version: wb-skill/v1
parameters:
  manual:
    type: bool
    description: Tag the snapshot as user-triggered (suffix '-manual'; separate retention bucket; default False = cron-driven.
    required: false
  push_remote:
    type: bool
    description: False forces local-only. None (default/scheduled) uploads only when a repo and backups.github.allow_unencrypted_private_content=true are configured. True without that opt-in requires exact high-risk per-invocation consent.
    required: false
mutates_state: true
retry_policy: manual
skill_name: data_backup
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
