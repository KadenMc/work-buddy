---
name: Backup Restore Directions
kind: directions
description: Restore work-buddy's vital SQLite databases from a local or remote snapshot, with manifest validation and migration forward-roll.
trigger: User wants to restore the task store / projects / messages / threads DBs from a snapshot. Use for recovering after data loss, onboarding a fresh-installed machine, or time-travelling to a past state.
command: wb-backup-restore
tags:
- backups
- restore
- disaster-recovery
- fresh-install
- slash-command
aliases:
- wb-backup-restore
- restore backup
- restore snapshot
- disaster recovery
- fresh install restore
parents:
- architecture/backups
- architecture/backups
---

Restore work-buddy's vital SQLite databases from a snapshot. Use for:

- Recovering after a data-loss incident (live DB is corrupt or empty).
- Onboarding a fresh-installed machine (pull the latest snapshot from GitHub Releases and resume).
- Time-travel to investigate a past state.

The pipeline is safe by default:

1. **Validates the snapshot's manifest** -- refuses to restore a snapshot whose work-buddy commit or schema version is newer than the running code (forward-time-travel guard). Use `force=true` only if you understand why this guard exists.
2. **Unpacks into a staging directory** so the live DB is untouched until step 5.
3. **Runs the migration ladder forward** on the staging DBs -- a snapshot taken at an older schema version lands at the current code's schema transparently. See `architecture/migrations`.
4. **Verifies integrity** via `PRAGMA integrity_check` + `PRAGMA foreign_key_check` per DB. Refuses to swap on failure.
5. **Atomic swap.** The current `.data/db/` is moved to `.data/db.pre_restore_<ts>/` (auto-rollback path), then staging takes its place.

## How

List available snapshots first:

    mcp__work-buddy__wb_run("data_backup_list", {"include_remote": true})

Then restore by snapshot ID:

    mcp__work-buddy__wb_run("data_restore", {"snapshot_id": "<snapshot-id>"})

For a remote snapshot (downloads via `gh release download` first):

    mcp__work-buddy__wb_run("data_restore", {
        "snapshot_id": "<snapshot-id>",
        "from_remote": true
    })

## Recovery from a wiped machine

1. Clone work-buddy fresh, restore `config.local.yaml`.
2. Make sure `gh` is installed + authenticated (the `github_backups` Requirements in Settings will guide setup).
3. `data_backup_list({"include_remote": true})` to see remote snapshots.
4. `data_restore({"snapshot_id": "<latest>", "from_remote": true})`.

Total downtime: a few minutes once `gh` is set up.

## What gets restored

The four vital DBs declared in `work_buddy/backups/local.py` as `VITAL_DBS`: `tasks` (full task store -- history, tags, action items, sync status, soft-deleted rows), `projects`, `messages`, `threads`.

**Not restored:** logs, retry queue (transient), notification store, artifacts directory. Reproducible from re-running the sidecar.

See `architecture/backups` for the full subsystem.
