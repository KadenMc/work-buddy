---
name: Data Backups
kind: concept
description: Off-machine snapshot + restore system for work-buddy's vital SQLite databases. Hot-backup -> tarball -> manifest -> GitHub Releases. Tiered retention, gh-CLI driven, integrates with the health Component system.
tags:
- backups
- snapshot
- restore
- sqlite
- github-releases
- retention
- manifest
- disaster-recovery
- data-safety
aliases:
- data backup
- backup system
- restore pipeline
- github releases backup
- snapshot retention
- manifest format
- disaster recovery
- data-backup
- data-restore
parents:
- architecture
- architecture
dev_notes: Centerpiece of the backups documentation cluster. Pairs with architecture/migrations (separate concern, used beyond restore). VITAL_DBS table is the single source of truth for which DBs are backed up; if a new vital DB is added (e.g. consent.db, conversations.db) this is the unit that needs the table updated. File pointers for each subsystem live next to the code in the relevant modules; this unit deliberately does not duplicate that file map -- search `work_buddy/backups/` and `work_buddy/health/` to discover.
---

Off-machine snapshot + restore for work-buddy's vital SQLite databases. Built on SQLite's hot-backup API, tarballed with a structured manifest, pushed to a user-owned private GitHub Releases bucket, and recoverable on a fresh-installed machine through a schema-aware restore pipeline.

Lives in `work_buddy/backups/`. The system has four moving parts (local snapshot, manifest, remote push, restore) plus a health-Component for setup and observability.

## Why it exists

The task store is the single source of truth for everything work-buddy knows about the user's work -- claims, archives, action items, tags, state history. A single bug that issues a wide-fanout `DELETE` against it (or a corrupted disk, or a fat-fingered `rm -rf .data/`) would be catastrophic and not recoverable from any other system surface. The backup system + the soft-delete discipline (see `tasks/task_delete`) are two halves of the same safety net: soft-delete prevents accidental destruction of *individual* rows; backups protect against *categorical* loss of the whole store.

Vital DBs that get backed up (declared in `work_buddy/backups/local.py` as `VITAL_DBS`):

| Logical name | On-disk file | Owner |
|---|---|---|
| `tasks` | `.data/db/tasks/task_metadata.db` | `obsidian/tasks-plugin` |
| `projects` | `.data/db/projects.db` | `projects/` |
| `messages` | `.data/db/messages.db` | `messaging/` |
| `threads` | `.data/db/threads.db` | `threads/` |
| `entities` | `.data/db/entities.db` | `entities/` |

The logical name is what appears in the manifest and the snapshot tag; the on-disk filename is preserved inside the tarball so restore can reconstruct the directory layout.

## Snapshot pipeline (`work_buddy/backups/local.py`)

1. For each vital DB, open it and call `sqlite3.Connection.backup(dest)`. This is SQLite's hot-backup API -- a page-by-page logical copy under the lock protocol that does not block writers and is WAL-coherent. Output: `.data/backups/<snapshot_id>/<dbname>.db`.
2. Write `MANIFEST.json` alongside.
3. Tar+gzip the directory via Python's `tarfile` stdlib (cross-platform, no shell-out).
4. Sweep retention (see Retention).
5. If `backups.github.repo` is configured and `gh` is authenticated, push to GitHub Releases (see Remote push).
6. Write `.data/backups/last_run.json` recording success/fail + duration + sizes. Health checks read this file -- they never hit GitHub on the hot path.

Snapshot IDs are ISO-timestamped: `snap-<utc-isoformat>`. Manual snapshots (triggered via `/wb-backup-now` or `data_backup(manual=True)`) get a `-manual` suffix and live in their own retention bucket.

## Manifest format (`work_buddy/backups/manifest.py`)

Keys:
- `snapshot_ts` -- ISO UTC timestamp of the snapshot.
- `work_buddy_version`, `work_buddy_commit`, `work_buddy_branch`, `work_buddy_dirty` -- code provenance at snapshot time. `work_buddy_dirty=True` flags an uncommitted working tree as an audit signal; does not block restore.
- `host` -- hostname of the snapshotting machine.
- `schema_versions` -- map of logical DB name -> `PRAGMA user_version` at snapshot time. Restore uses this to refuse forward-time travel and to drive forward-migration.
- `row_counts` -- map of table -> row count at snapshot time. Restore validates post-migration counts against this, with tolerance for migration-added rows.
- `manifest_version` -- integer; future-proofs the manifest format itself. Restore checks it and refuses unknown values.

## Retention (tiered, per-tier capped)

Sweep runs after every snapshot. Mirrors locally and remotely (the remote push uses `gh release delete` for out-of-bucket snapshots).

| Tier | Cadence | Cap |
|---|---|---|
| Hourly | every hour | 24 |
| Daily  | one per day | 7 |
| Weekly | one per ISO week | 4 |
| Monthly | one per calendar month | 12 |
| Annual | one per calendar year | unbounded |
| Manual | user-triggered | 20 (independent bucket) |

Steady-state local footprint at ~3 MB compressed per snapshot is ~156 MB across the ~52 retained slots. Manual snapshots are deliberately a small bucket -- they are *anchor points* a user takes before something risky, not archival.

Configured in `config.local.yaml` under `backups.github.retention.*`.

## Remote push (`work_buddy/backups/remote.py`)

The remote target is a *user-owned private GitHub repository*. Snapshots are uploaded as GitHub Release assets, one release per snapshot, tagged with the snapshot ID. We subprocess the `gh` CLI rather than embed PyGithub because:

- The user's existing GitHub credentials are managed by `gh`; we never touch a PAT.
- `gh release create <tag> <files>` is one-shot, idempotent on retry, and supports private repos natively.
- The `gh release list --json` query lets the restore pipeline enumerate remote snapshots without a Python GitHub client.

No encryption layer. A private GitHub repo is the same trust model as the user's other private code repositories -- the threat model is account compromise, not in-transit interception. Adding GPG encryption would buy nothing and add a key-management failure mode.

Fresh-repo gotcha: the first push to an empty repo errors with `Repository is empty`. The `fix_backup_repo_configured` fixer creates the repo with `gh repo create --private --add-readme` to seed the default branch.

## Restore pipeline (`work_buddy/backups/restore.py`)

`data_restore(snapshot_id)` (capability) executes:

1. Download `<tag>.tar.gz` from GitHub Releases into a staging directory.
2. Read `MANIFEST.json` and validate: `manifest_version` is recognized; for each DB, snapshot's `schema_versions[db]` <= code's max migration (forward-time-travel guard).
3. Unpack into `.data/db.staging_<ts>/`.
4. Open each staged DB through `MigrationRunner.run()` (see `architecture/migrations`) -- the ladder rolls the staged schema forward to current.
5. `PRAGMA integrity_check` + `PRAGMA foreign_key_check` per DB. Refuse on either failure.
6. Verify post-migration row counts match the manifest, with tolerance for migration-added rows.
7. Move current `.data/db/` to `.data/db.pre_restore_<ts>/` (auto-rollback safety net).
8. Move staging into place.

Steps 3-7 are reversible -- staging gets discarded on any failure and the live DB is never touched until step 8.

## Health system integration

Registered as a non-core opt-in Component `github_backups` (see `architecture/health`). Three Requirements with their own Fixers:

| Requirement | Fix kind | Fixer behaviour |
|---|---|---|
| `gh-cli-installed` | `agent_handoff` | Spawns a Claude Code session that walks the user through OS-appropriate install. |
| `gh-authenticated` | `agent_handoff` | Walks through `gh auth login --web`. |
| `repo-configured` | `input_required` | Form for repo name, calls `gh repo create --private --add-readme` if absent, writes `backups.github.repo` to `config.local.yaml`. |

The Component declares one custom check (`check_github_backup_freshness`) that reads `.data/backups/last_run.json` and returns success/warning/failure based on whether the last snapshot landed inside the configured cadence window. It never polls GitHub directly.

A `domain:backups` entry in `work_buddy/control/graph_static.py` makes the Component surface in the Settings tab's domain list. No frontend work beyond adding `"domain:backups"` to `domainOrder` -- the card auto-renders from the control graph (see `architecture/control-graph`).

## Cron + slash commands

- `sidecar_jobs/data-backup.md` -- hourly cron, calls `data_backup` capability. Skips silently if the Component is unwanted or its Requirements are unmet.
- `/wb-backup-now` -- manual one-off snapshot. Used as an anchor point before a risky operation.
- `/wb-backup-restore [snapshot-id]` -- list remote snapshots or restore a specified one.

There are no `/wb-backup-setup`, `/wb-backup-status`, or `/wb-backup-config` slash commands by design -- those surface via the Settings tab's auto-rendered card. The slash-command surface is reserved for the two recurring user-initiated operations (snapshot now, restore).

## Capabilities (registered in `work_buddy/mcp_server/registry.py`)

- `data_backup(manual: bool = False)` -- take a snapshot, push to remote.
- `data_backup_list()` -- list local + remote snapshots with sizes and timestamps.
- `data_restore(snapshot_id: str, force: bool = False)` -- restore from a given snapshot.

## See also

- `architecture/migrations` -- the MigrationRunner schema-version ladder that restore depends on for forward-rolling a staged DB.
- `architecture/health`, `architecture/control-graph` -- how the Component and its Requirements surface in Settings.
- `tasks/task_delete` -- the soft-delete safety pattern that complements off-machine backups.
