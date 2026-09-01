---
schedule: "0 * * * *"
recurring: true
type: capability
capability: data_backup
params: {}
---
Hourly snapshot of work-buddy's vital SQLite databases.

Pipeline:

1. `sqlite3.Connection.backup` per vital DB (including Tasks, Projects,
   Contracts, Personal Knowledge, Messages, and Threads) → page-by-page
   consistent copy, no writer blocking.
2. Bundle + manifest into `<data_root>/backups/snap-<isots>/work-buddy-backup.tar.gz`.
3. Keep the archive local unless the `github_backups` Component is configured
   **and** `backups.github.allow_unencrypted_private_content` is exactly
   `true`. A configured repository alone never authorizes upload. When opted
   in, push the tarball as a GitHub Release asset and mirror local retention.
4. Write `<data_root>/backups/last_run.json` so the Component health
   check can surface freshness without hitting the GitHub API.

The capability handles "stay local unless explicitly opted in" internally.
Running this job before remote-private-content authorization just produces
local snapshots, which is the safe default.

Configure the remote target in `config.local.yaml`:

    backups:
      github:
        repo: "username/work-buddy-data"
        cadence_minutes: 60
        # The archive is unencrypted and contains private domain data.
        allow_unencrypted_private_content: false

Use the Settings tab → Backups → GitHub Releases Backup card to set the repo.
After reviewing the privacy tradeoff, set the opt-in to `true` in
`config.local.yaml` if unattended remote uploads are wanted.
