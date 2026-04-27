---
schedule: "5 4 * * *"  # daily at 04:05 — off-hours, no clash with 7 * * * * journal scan
recurring: true
type: capability
capability: triage_pool_sweep
params: {}
---
Daily liveness sweep over the triage pool (Slice 1).

Walks every pending `PoolEntry`:

- **TTL expiry**: entries past their `expires_at` (set at creation
  from the source descriptor's TTL — journal 5d, chrome 2d, inline
  null) transition `pending → stale`.
- **Quarantine triggers**: each entry's source descriptor declares
  trigger names (`source_removed`, `source_edited_beyond_match`,
  `tag_removed`). The first one that fires sets the
  `quarantine_reason` and transitions `pending → quarantined`.

Quarantine takes precedence over stale (more specific signal — the
source is GONE, not just old).

Non-destructive: state changes only; every entry stays on disk for
audit. The Review tab pulls only `state == "pending"` so stale and
quarantined entries silently leave the active surface.

Cadence (`schedule:` above) is a job-level concern — change it here
without touching the capability. The capability itself has no
opinion on how often it runs.

Manual rehearsal:
```
mcp__work-buddy__wb_run("triage_pool_sweep", {"dry_run": true})
```
