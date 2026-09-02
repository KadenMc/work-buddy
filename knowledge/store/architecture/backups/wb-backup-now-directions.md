---
name: Backup Now Directions
kind: directions
description: Take an immediate manual snapshot of work-buddy's vital SQLite databases as an anchor point before a risky operation.
trigger: 'User wants a fresh point-in-time snapshot of the task store and the other vital SQLite databases. Typical anchor points: right before a destructive operation, before swapping branches, before running a long script that writes to the store, or just because it''s been a while.'
command: wb-backup-now
tags:
- backups
- manual-snapshot
- anchor-point
- slash-command
aliases:
- wb-backup-now
- manual backup
- snapshot now
- anchor snapshot
parents:
- architecture/backups
- architecture/backups
---

Take an immediate manual snapshot of work-buddy's vital SQLite databases. Useful right before doing something risky (a destructive operation, an experimental migration, etc.) so a known-good point-in-time exists to restore to if things go sideways.

Manual snapshots get a `-manual` suffix on their snapshot ID and live in a separate retention bucket from the hourly rolling snapshots, so this won't be swept away by routine cron churn (cap: 20 manual snapshots; deliberately small -- anchor points, not archival).

The snapshot stays local by default. It also pushes to GitHub Releases only
when `backups.github.repo` is configured, `gh` is authenticated, and
`backups.github.allow_unencrypted_private_content` is exactly `true`. A repo
setting names a destination but does not authorize private-content uploads.

For one exceptional upload without the persistent opt-in, call with
`push_remote: true`. That path requires an exact high-risk, per-invocation
consent decision before the snapshot is created or sent. The archive is not
encrypted and includes private domain data and portable scoped Truth exports.

## How

Call the capability directly:

    mcp__work-buddy__wb_run("data_backup", {"manual": true})

The response shows the snapshot ID, tarball path, size, and whether the remote push succeeded.

## When to use

- About to run a `reload_capability_data` (or restart) after editing a sensitive capability.
- About to run `task_archive` or another high-fan-out task-mutating operation.
- About to swap branches, run a long-running script that writes to the store, or restore an older state.
- Just want a fresh off-machine copy because it has been a while.

For automatic protection between manual snapshots, the hourly cron (`sidecar_jobs/data-backup.md`) takes a rolling snapshot every hour. See `architecture/backups` for the full subsystem.
