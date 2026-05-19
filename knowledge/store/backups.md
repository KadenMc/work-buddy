---
name: Backups
kind: concept
description: Data-backup capabilities — snapshot, restore, and remote sync of work-buddy's databases
tags:
- backups
- backup
- snapshot
- restore
---

Work-buddy periodically snapshots its vital SQLite databases (task metadata, projects, messages, threads), tar+gzips them with a manifest, and optionally pushes to a remote GitHub Releases repo. Backups run on an hourly sidecar cron and on demand via the `/wb-backup-now` and `/wb-backup-restore` slash commands.
