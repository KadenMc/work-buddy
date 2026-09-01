---
name: Data Backups
kind: concept
description: Local and opt-in off-machine restore coverage for ordinary vital SQLite databases and portable Truth payloads, plus local-only authorized Journal/Sources checkpoints.
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
dev_notes: |-
  Centerpiece of the backups documentation cluster. Pairs with architecture/migrations, which is a separate concern used beyond restore. VITAL_DBS is the ordinary snapshot inventory, not an inventory of every machine database: native Journal and retained Sources use the separate authorized sensitive-checkpoint path. Truth coverage has two parts: the machine registry is a vital database, and registered scoped stores contribute dynamic portable payloads. If a new ordinary vital DB, sensitive cohort, or dynamic recovery source is added, this unit needs the corresponding inventory updated. File pointers for each subsystem live next to the code in the relevant modules. Search `work_buddy/backups/` and `work_buddy/health/` to discover them.

  Remote retention (`prune_remote_snapshots`) buckets releases by `parse_snapshot_ts(tag)`, never by the `gh` release `createdAt` field: `createdAt` is the date of the commit a release tag points at, and in a data-only backup repo every tag points at the single seed commit -- so `createdAt` is identical across every release, and keying retention on it collapses all rolling snapshots into one bucket (the sweep then deletes all but one off-machine copy). `list_remote_snapshots` surfaces `publishedAt` (the real push time) for display only.
---

Snapshot + restore for work-buddy's ordinary vital SQLite databases and registered scoped Truth stores. Machine databases use SQLite's hot-backup API. Truth stores contribute portable recovery payloads. Everything is tarballed locally with a structured manifest and is recoverable through the schema-aware restore pipeline and Truth import library. Upload to a user-owned private GitHub Releases bucket is optional and requires explicit authorization because the archive is unencrypted and contains private domain data. Native Journal and retained Sources use a separate authorized, local-only sensitive checkpoint.

Lives in `work_buddy/backups/`. The ordinary snapshot system has four moving parts (local snapshot, manifest, remote push, restore) plus a health-Component for setup and observability. The sensitive path adds guarded Sources export, coordinated Journal checkpoint, verification, and isolated restore rehearsal.

## Why it exists

Work-buddy's vital databases and scoped Truth stores hold durable state that cannot be reconstructed from another system surface. A wide-fanout deletion, corrupted disk, or accidental removal of `.data/` or a Co-work `.wbuddy/cowork/` sidecar could otherwise cause categorical data loss. Soft-delete protects individual task rows. Backups protect the durable stores as a whole.

Vital DBs that get backed up (declared in `work_buddy/backups/local.py` as `VITAL_DBS`):

| Logical name | On-disk file | Owner |
|---|---|---|
| `tasks` | `.data/db/task_metadata.db` | `tasks/` |
| `projects` | `.data/db/projects.db` | `projects/` |
| `contracts` | `.data/db/contracts.db` | `contracts_domain/` |
| `personal_knowledge` | `.data/db/personal_knowledge.db` | `knowledge/personal/` |
| `installed_authority` | `.data/db/installed_authority.db` | `installed_authority.py` |
| `messages` | `.data/db/messages.db` | `messaging/` |
| `threads` | `.data/db/threads.db` | `threads/` |
| `entities` | `.data/db/entities.db` | `entities/` |
| `settings` | `.data/db/settings.db` | `settings` |
| `truth_registry` | `<data_root>/db/truth_registry.db` | `truth/registry.py` |
| `agent_execution` | `.data/db/agent_execution.db` | `agent_execution/` |
| `cowork_conversation_source_dependencies` | `.data/db/cowork_conversation_source_dependencies.db` | `cowork/` |
| `task_note_migration` | `.data/db/task_note_migration.db` | `task_notes/` |

The logical name is what appears in the manifest and the snapshot tag. The on-disk filename is preserved inside the tarball so restore can reconstruct the directory layout.

`truth_registry` is only the machine inventory of known scoped stores. Authoritative claims live beside the material they govern in the canonical `.wbuddy/cowork/` sidecar. Those sidecars are covered dynamically through portable exports, not by adding their live SQLite databases to `VITAL_DBS`.

Native `journal_capture.db` and the retained Sources store are intentionally
absent from `VITAL_DBS`. They form one sensitive recovery cohort so an exact
Sources occurrence cannot be restored independently of the Journal rows that
depend on it. They are never added to the ordinary remote-eligible tarball.

## Sensitive Journal and Sources checkpoints

Sensitive backup is an explicit two-step local operation. First,
`source_maintenance_operator(action="export")` produces a content-carrying
Sources archive and records the issued offline copy behind its high-consent
boundary. Then `data_sensitive_checkpoint` verifies that exact receipt and
adds a SQLite-hot `journal_capture.db` member plus
`SENSITIVE-MANIFEST.json` in the same directory. The Sources archive is not
copied or renamed, and the manifest is always `remoteEligible: false`.

`verify_sensitive_checkpoint` checks member digests, the Journal SQLite
integrity/schema, and the authorized Sources export receipt.
`rehearse_sensitive_checkpoint_restore` requires explicit operational-state
import authorization and restores both members into a fresh isolated
destination. It verifies identity-preserving import counts and every
Journal-to-Source dependency before publication. Rehearsal is not an in-place
or live restore operator; callers remove the isolated destination after
capturing its content-free receipt. Sensitive checkpoints are not consumed by
ordinary `data_restore`.

## Snapshot pipeline (`work_buddy/backups/local.py`)

1. Open the Truth machine registry and refresh every known store. For each reachable store, stage `truth_stores/<store_id>/store.yaml`, `truth_stores/<store_id>/claims.jsonl`, and the identity-bound `truth_stores/<store_id>/document-causality.json` companion. Record unreachable stores, a missing causality authority, and export errors instead of silently omitting them.
2. For each vital DB, open it and call `sqlite3.Connection.backup(dest)`. This is SQLite's hot-backup API, a page-by-page logical copy under the lock protocol that does not block writers and is WAL-coherent. Output: `.data/backups/<snapshot_id>/<dbname>.db`.
3. Write `MANIFEST.json` alongside the machine database snapshots and portable Truth payloads.
4. Tar+gzip the directory via Python's `tarfile` standard library.
5. Sweep retention (see Retention).
6. Push to GitHub Releases only when a destination repo is configured and the explicit private-content policy allows it (see Remote push). Repository configuration alone never authorizes upload.
7. Write `.data/backups/last_run.json` with the snapshot and remote-push outcome. The returned result and `MANIFEST.json` carry detailed Truth coverage. Health checks read the last-run sentinel and never hit GitHub on the hot path.

Snapshot IDs are ISO-timestamped: `snap-<utc-isoformat>`. Manual snapshots (triggered via `/wb-backup-now` or `data_backup(manual=True)`) get a `-manual` suffix and live in their own retention bucket.

## Manifest format (`work_buddy/backups/manifest.py`)

Keys:

- `snapshot_ts` -- ISO UTC timestamp of the snapshot.
- `work_buddy_version`, `work_buddy_commit`, `work_buddy_branch`, `work_buddy_dirty` -- code provenance at snapshot time. `work_buddy_dirty=True` flags an uncommitted working tree as an audit signal; does not block restore.
- `host` -- hostname of the snapshotting machine.
- `schema_versions` -- map of logical DB name -> `PRAGMA user_version` at snapshot time. Restore uses this to refuse forward-time travel and to drive forward-migration.
- `row_counts` -- map of table -> row count at snapshot time. Restore validates counts after schema upgrade against this, with tolerance for migration-added rows.
- `truth_stores` -- one entry per registered store, including its permanent identity, registered path, profile, reachability, and `backup_status`. Included entries name the portable Truth and document-causality members plus their outer/inner hashes. Unreachable and errored entries carry an explicit reason.
- `manifest_version` -- integer; future-proofs the manifest format itself. Restore checks it and refuses unknown values.

## Portable Truth coverage

Registry discovery turns the variable set of scoped `.wbuddy/cowork/` sidecars into deterministic
recovery members. Each included store contributes exactly
`truth_stores/<store_id>/store.yaml` and
`truth_stores/<store_id>/claims.jsonl`, and
`truth_stores/<store_id>/document-causality.json`. The profile preserves
permanent identity and policy. The JSONL stream preserves the lossless ordered
ledger representation used by Truth import. The causality companion preserves
domain/document bindings, change intents/receipts, and projection cursors; it
is bound to the same permanent store ID and imported only into a clean staged
target whose document IDs match the portable Truth ledger.

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

There is no encryption layer. The archive can contain Projects descriptions,
Contracts, Personal Knowledge, Tasks, messages, identities, settings, and
portable scoped Truth exports. Therefore `backups.github.repo` only names the
destination. Scheduled/default backup calls remain local-only unless
`backups.github.allow_unencrypted_private_content` is the YAML boolean `true`;
strings such as `"true"` fail closed. An explicit `push_remote=True` without
the persistent opt-in is allowed only through a high-risk, per-invocation
consent prompt bound to the repository and content class. Consent is checked
before the local snapshot is created, so a denied request cannot accidentally
upload or create duplicate retry snapshots. The low-level `push_snapshot`
boundary rechecks the persistent flag or the exact consumed per-invocation
authorization, so another production caller cannot bypass the public policy by
calling the transport helper directly.

Fresh-repo gotcha: the first push to an empty repo errors with `Repository is empty`. The `fix_backup_repo_configured` fixer creates the repo with `gh repo create --private --add-readme` to seed the default branch.

## Restore pipeline (`work_buddy/backups/restore.py`)

`data_restore(snapshot_id, from_remote=False)` (capability) executes:

1. Resolve a local snapshot ID or absolute snapshot-directory path by default. When `from_remote=true`, download the GitHub Release tarball into a temporary local snapshot directory first.
2. Read `MANIFEST.json` and validate: `manifest_version` is recognized; for each DB, snapshot's `schema_versions[db]` <= code's max migration (forward-time-travel guard).
3. Unpack into `.data/db.staging_<ts>/`. Remove `truth_stores/` from the machine database staging tree so portable scoped payloads cannot be moved into `<data_root>/db/`.
4. Open each staged DB through its migration authority (see `architecture/migrations`) -- the ladder rolls the staged schema forward to current. The Settings database and Truth registry use their own versioned ladders and the same forward-version guard.
5. `PRAGMA integrity_check` + `PRAGMA foreign_key_check` per DB. Refuse on either failure.
6. Verify row counts after schema upgrade match the manifest, with tolerance for migration-added rows.
7. Write `source_foundation_restore_pending.json` into the validated staging
   directory, then move current `.data/db/` to `.data/db.pre_restore_<ts>/`
   (auto-rollback safety net).
8. Move staging into place. The marker becomes live in the same rename, so a
   completed swap has no unfenced crash window.

Steps 3-6 are staging-only. The live DB is first touched in step 7, immediately
before the directory swap, and is retained as the pre-restore rollback copy.

Before publication, restore conservatively unions irreversible rows from the
live and snapshot `installed_authority.db` ledgers. When a current installation
exists, it also snapshots the live Journal, local identity, Co-work
conversation-source dependencies, and retained Sources into staging instead
of rolling those Source Foundation authorities backward with an ordinary
archive. On a fresh machine those live-only cohorts remain absent, so the
recovery fence cannot clear until their explicit recovery and reconciliation
requirements are satisfied.

The restore marker is a fail-closed authority fence, not a warning. Normal
Source Foundation mutations, background delivery, model/provider dispatch,
and new snapshots remain paused while it exists. Read-only status remains
available when existing state can be validated. The high-consent
`source_foundation_restore_operator` compares identity, Agent
Execution/Sources reservations and outcomes, retained Sources blob boundaries,
Co-work conversation-source dependencies, Journal/task-note state, scoped
Truth/causality identity, and Hindsight projection recovery. It never replays a
`possibly_sent` transport. It clears only a fully reconciled cohort and archives
the marker as a receipt; partial evidence leaves the system fenced.

The Co-work conversation-source dependency database is hot-backed for disaster
recovery. A local restore instead keeps its live copy aligned with the retained
conversation database, which lives outside the machine `db/` swap; cohort
reconciliation verifies every dependency against that retained history.

The snapshot tarball retains its `truth_stores/` members for explicit scoped recovery. `data_restore` does not place those payloads automatically because the destination scope and duplicate store identity policy require a deliberate choice. Scoped import is available only through `work_buddy.truth.export.import_store`, which accepts the optional causality companion and digest as one staged import. No `truth_store_import` MCP capability or `wbuddy truth import` verb is registered.

## Health system integration

Registered as a non-core opt-in Component `github_backups` (see `architecture/health`). Three Requirements with their own Fixers:

| Requirement | Fix kind | Fixer behaviour |
|---|---|---|
| `gh-cli-installed` | `agent_handoff` | Spawns a Claude Code session that walks the user through OS-appropriate install. |
| `gh-authenticated` | `agent_handoff` | Walks through `gh auth login --web`. |
| `repo-configured` | `input_required` | Form for repo name, calls `gh repo create --private --add-readme` if absent, writes `backups.github.repo` to `config.local.yaml`. |
| `private-content-opt-in` | `none` | Requires `backups.github.allow_unencrypted_private_content` to be exactly `true`. The user must review the unencrypted archive scope and edit local config deliberately; repository setup does not flip it. |

The Component declares one custom check (`check_github_backup_freshness`) that reads `.data/backups/last_run.json` and returns success/warning/failure based on whether the last snapshot landed inside the configured cadence window. It never polls GitHub directly.

A `domain:backups` entry in `work_buddy/control/graph_static.py` makes the Component surface in the Settings tab's domain list. No frontend work beyond adding `"domain:backups"` to `domainOrder` -- the card auto-renders from the control graph (see `architecture/control-graph`).

## Cron + slash commands

- `sidecar_jobs/data-backup.md` -- hourly cron, calls `data_backup` capability. Local hot snapshots always run; remote push occurs only with the explicit persistent private-content opt-in.
- `/wb-backup-now` -- manual one-off snapshot. Used as an anchor point before a risky operation.
- `/wb-backup-restore [snapshot-id]` -- list remote snapshots or restore a specified one.

There are no `/wb-backup-setup`, `/wb-backup-status`, or `/wb-backup-config` slash commands by design -- those surface via the Settings tab's auto-rendered card. The slash-command surface is reserved for the two recurring user-initiated operations (snapshot now, restore).

## Capabilities (registered in `work_buddy/mcp_server/registry.py`)

- `data_backup(manual: bool = False, push_remote: bool | None = None)` -- take a snapshot. Default/scheduled calls are local-only unless the persistent opt-in is true; an exceptional explicit remote request uses exact per-invocation consent.
- `data_backup_list(include_remote: bool = False)` -- list local snapshots, optionally including the configured remote releases.
- `data_sensitive_checkpoint(...)` -- seal and verify a local-only Journal member beside an already authorized Sources export; see `backups/data_sensitive_checkpoint` for the receipt parameters.
- `data_restore(snapshot_id: str, from_remote: bool = False, force: bool = False)` -- restore a local snapshot by default or explicitly download a remote release first.

## See also

- `architecture/migrations` -- the MigrationRunner schema-version ladder that restore depends on for forward-rolling a staged DB.
- `architecture/source-foundation`, `backups/data_sensitive_checkpoint` -- the sensitive Sources/Journal boundary and its guarded capability contract.
- `architecture/health`, `architecture/control-graph` -- how the Component and its Requirements surface in Settings.
- `tasks/task_delete` -- the soft-delete safety pattern that complements off-machine backups.
