---
schedule: "0 * * * *"
recurring: true
type: capability
capability: data_backup
params: {}
---
Hourly snapshot of work-buddy's vital SQLite databases.

Pipeline:

1. `sqlite3.Connection.backup` per vital DB (`task_metadata`,
   `projects`, `messages`, `threads`) → page-by-page consistent
   copy, no writer blocking.
2. Bundle + manifest into `<data_root>/backups/snap-<isots>/work-buddy-backup.tar.gz`.
3. If the `github_backups` Component is configured (`backups.github.repo`
   set, gh CLI authenticated), push the tarball as a GitHub Release
   asset. Mirror the local rolling retention on the remote.
4. Write `<data_root>/backups/last_run.json` so the Component health
   check can surface freshness without hitting the GitHub API.

The capability handles "skip silently if unconfigured" internally —
running this job before the user has set up GitHub backups just
produces local snapshots, which is the safe default.

Configure the remote target in `config.local.yaml`:

    backups:
      github:
        repo: "username/work-buddy-data"
        cadence_minutes: 60

Or use the Settings tab → Backups → GitHub Releases Backup card to
set the repo via the requirement fixer.
