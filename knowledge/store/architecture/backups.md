---
name: Data Backups
kind: concept
description: Off-machine snapshots and restore for vital SQLite databases plus portable Truth store recovery payloads. Hot-backup -> tarball -> manifest -> GitHub Releases. Tiered retention, gh-CLI driven, integrates with the health Component system.
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
dev_notes: |-
  Centerpiece of the backups documentation cluster. Pairs with architecture/migrations, which is a separate concern used beyond restore. VITAL_DBS is the single source of truth for machine databases. Truth coverage has two parts: the machine registry is a vital database, and registered scoped stores contribute dynamic portable payloads. If a new vital DB or dynamic recovery source is added, this unit needs the corresponding inventory updated. File pointers for each subsystem live next to the code in the relevant modules. Search `work_buddy/backups/` and `work_buddy/health/` to discover them.

  Remote retention (`prune_remote_snapshots`) buckets releases by `parse_snapshot_ts(tag)`, never by the `gh` release `createdAt` field: `createdAt` is the date of the commit a release tag points at, and in a data-only backup repo every tag points at the single seed commit -- so `createdAt` is identical across every release, and keying retention on it collapses all rolling snapshots into one bucket (the sweep then deletes all but one off-machine copy). `list_remote_snapshots` surfaces `publishedAt` (the real push time) for display only.
---

Off-machine snapshot + restore for work-buddy's vital SQLite databases and registered scoped Truth stores. Machine databases use SQLite's hot-backup API. Truth stores contribute portable recovery payloads. Everything is tarballed with a structured manifest, pushed to a user-owned private GitHub Releases bucket, and recoverable on a freshly installed machine through the schema-aware restore pipeline and the Truth import library.

Lives in `work_buddy/backups/`. The system has four moving parts (local snapshot, manifest, remote push, restore) plus a health-Component for setup and observability.

## Why it exists

Work-buddy's vital databases and scoped Truth stores hold durable state that cannot be reconstructed from another system surface. A wide-fanout deletion, corrupted disk, or accidental removal of `.data/` or a `.wb-truth/` sidecar could otherwise cause categorical data loss. Soft-delete protects individual task rows. Backups protect the durable stores as a whole.

Vital DBs that get backed up (declared in `work_buddy/backups/local.py` as `VITAL_DBS`):

| Logical name | On-disk file | Owner |
|---|---|---|
| `tasks` | `.data/db/tasks/task_metadata.db` | `obsidian/tasks-plugin` |
| `projects` | `.data/db/projects.db` | `projects/` |
| `messages` | `.data/db/messages.db` | `messaging/` |
| `threads` | `.data/db/threads.db` | `threads/` |
| `entities` | `.data/db/entities.db` | `entities/` |
| `settings` | `.data/db/settings/settings.db` | `settings` |
| `truth_registry` | `<data_root>/db/truth_registry.db` | `truth/registry.py` |

The logical name is what appears in the manifest and the snapshot tag. The on-disk filename is preserved inside the tarball so restore can reconstruct the directory layout.

`truth_registry` is only the machine inventory of known scoped stores. Authoritative claims remain in `.wb-truth/` sidecars beside the material they govern. Those sidecars are covered dynamically through portable exports, not by adding their live SQLite databases to `VITAL_DBS`.

## Snapshot pipeline (`work_buddy/backups/local.py`)

1. Open the Truth machine registry and refresh every known store. For each reachable store, stage `truth_stores/<store_id>/store.yaml` and `truth_stores/<store_id>/claims.jsonl`. Record unreachable stores and export errors instead of silently omitting them.
2. For each vital DB, open it and call `sqlite3.Connection.backup(dest)`. This is SQLite's hot-backup API, a page-by-page logical copy under the lock protocol that does not block writers and is WAL-coherent. Output: `.data/backups/<snapshot_id>/<dbname>.db`.
3. Write `MANIFEST.json` alongside the machine database snapshots and portable Truth payloads.
4. Tar+gzip the directory via Python's `tarfile` standard library.
5. Sweep retention (see Retention).
6. If `backups.github.repo` is configured and `gh` is authenticated, push to GitHub Releases (see Remote push).
7. Write `.data/backups/last_run.json` with the snapshot and remote-push outcome. The returned result and `MANIFEST.json` carry detailed Truth coverage. Health checks read the last-run sentinel and never hit GitHub on the hot path.

Snapshot IDs are ISO-timestamped: `snap-<utc-isoformat>`. Manual snapshots (triggered via `/wb-backup-now` or `data_backup(manual=True)`) get a `-manual` suffix and live in their own retention bucket.

## Manifest format (`work_buddy/backups/manifest.py`)

Keys:

- `snapshot_ts` -- ISO UTC timestamp of the snapshot.
- `work_buddy_version`, `work_buddy_commit`, `work_buddy_branch`, `work_buddy_dirty` -- code provenance at snapshot time. `work_buddy_dirty=True` flags an uncommitted working tree as an audit signal; does not block restore.
- `host` -- hostname of the snapshotting machine.
- `schema_versions` -- map of logical DB name -> `PRAGMA user_version` at snapshot time. Restore uses this to refuse forward-time travel and to drive forward-migration.
- `row_counts` -- map of table -> row count at snapshot time. Restore validates counts after schema upgrade against this, with tolerance for migration-added rows.
- `truth_stores` -- one entry per registered store, including its permanent identity, registered path, profile, reachability, and `backup_status`. Included entries name the portable members and export hash. Unreachable and errored entries carry an explicit reason.
- `manifest_version` -- integer; future-proofs the manifest format itself. Restore checks it and refuses unknown values.

## Portable Truth coverage

Registry discovery turns a variable set of scoped `.wb-truth/` stores into deterministic recovery members. Each included store contributes exactly `truth_stores/<store_id>/store.yaml` and `truth_stores/<store_id>/claims.jsonl`. The profile preserves permanent identity and policy. The JSONL stream preserves the lossless ordered ledger representation used by Truth import.

The backup pipeline never copies a scoped store's live `store.db`. This avoids treating a raw database image as the portable contract and avoids copying a database outside its own transaction protocol. If a store is unreachable, its manifest row is marked `unreachable`. If validation or export fails, the partial staged directory is removed and its row is marked `error`. Successful rows are marked `included` and carry the exported stream hash.

## Retention (tiered, per-tier capped)

Sweep runs after every snapshot, mirrored locally and remotely. Both sweeps bucket a snapshot by the timestamp encoded in its `snap-<isots>` id/tag -- never by a filesystem mtime or a GitHub release's `createdAt` -- so the local set and the remote set converge on the same tiered selection. The remote sweep deletes out-of-bucket releases with `gh release delete`.

| Tier | Cadence | Cap |
|---|---|---|
| Hourly | every hour | 24 |
| Daily  | one per day | 7 |
| Weekly | one per ISO week | 4 |
| Monthly | one per calendar month | 12 |
| Annual | one per calendar year | unbounded |
| Manual | user-triggered | 20 (independent bucket) |

Steady-state local footprint at ~3 MB compressed per snapshot is ~156 MB across the ~52 retained slots. Manual snapshots are deliberately a small bucket -- they are *anchor points* a user takes before something risky, not archival.

The tier caps are defined by the `RETENTION` dict in `work_buddy/backups/local.py`.

## Remote push (`work_buddy/backups/remote.py`)

The remote target is a *user-owned private GitHub repository*. Snapshots are uploaded as GitHub Release assets, one release per snapshot, tagged with the snapshot ID. We subprocess the `gh` CLI rather than embed PyGithub because:

- The user's existing GitHub credentials are managed by `gh`; we never touch a PAT.
- `gh release create` / `gh release upload` support private repos natively and need no Python GitHub client.
- The `gh release list --json` query lets the restore pipeline enumerate remote snapshots without a Python GitHub client.

Transient-fault handling: `push_snapshot` retries a push that fails with a network/DNS fault (e.g. intermittent resolution of `uploads.github.com`) up to three attempts with a short backoff -- well inside the hourly cron window. Permanent faults (gh missing, unauthenticated) are not retried. `gh release create` uploads the asset after creating the release object; if an earlier attempt created the release but its asset upload failed, the retry detects the "already exists" error and falls back to `gh release upload --clobber`, so a retried push converges instead of looping.

No encryption layer. A private GitHub repo is the same trust model as the user's other private code repositories -- the threat model is account compromise, not in-transit interception. Adding GPG encryption would buy nothing and add a key-management failure mode.

Fresh-repo gotcha: the first push to an empty repo errors with `Repository is empty`. The `fix_backup_repo_configured` fixer creates the repo with `gh repo create --private --add-readme` to seed the default branch.

## Restore pipeline (`work_buddy/backups/restore.py`)

`data_restore(snapshot_id)` (capability) executes:

1. Download `<tag>.tar.gz` from GitHub Releases into a staging directory.
2. Read `MANIFEST.json` and validate: `manifest_version` is recognized; for each DB, snapshot's `schema_versions[db]` <= code's max migration (forward-time-travel guard).
3. Unpack into `.data/db.staging_<ts>/`. Remove `truth_stores/` from the machine database staging tree so portable scoped payloads cannot be moved into `<data_root>/db/`.
4. Open each staged DB through its migration authority (see `architecture/migrations`) -- the ladder rolls the staged schema forward to current. The Settings database and Truth registry use their own versioned ladders and the same forward-version guard.
5. `PRAGMA integrity_check` + `PRAGMA foreign_key_check` per DB. Refuse on either failure.
6. Verify row counts after schema upgrade match the manifest, with tolerance for migration-added rows.
7. Move current `.data/db/` to `.data/db.pre_restore_<ts>/` (auto-rollback safety net).
8. Move staging into place.

Steps 3-7 are reversible -- staging gets discarded on any failure and the live DB is never touched until step 8.

The snapshot tarball retains its `truth_stores/` members for explicit scoped recovery. `data_restore` does not place those payloads automatically because the destination scope and duplicate store identity policy require a deliberate choice. Scoped import is available only through `work_buddy.truth.export.import_store`. No `truth_store_import` MCP capability or `wbuddy truth import` verb is registered.

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
