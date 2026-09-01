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

Work-buddy periodically snapshots its vital SQLite databases (including Tasks,
Projects, Contracts, Personal Knowledge, Messages, and Threads), tar+gzips them
with a manifest, and can push to a remote GitHub Releases repo. Backups run on
an hourly sidecar cron and on demand via `/wb-backup-now`. Local snapshots are
the default; unattended upload of the unencrypted private archive requires the
explicit `backups.github.allow_unencrypted_private_content: true` opt-in.
