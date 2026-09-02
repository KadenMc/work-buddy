---
schedule: "*/10 * * * *"  # every 10 minutes
recurring: true
jitter_seconds: 180  # spread 10-minute pile-ups (especially at :00 / :30)
type: capability
capability: inline_sync
params: {}
enabled: false
---
Retired with Obsidian inline `#wb/cmd/*` scanning. Native selections, task
records, and explicit actions replace file-tag discovery; there is no
replacement background scanner. The legacy implementation remains importable
for migration inspection but this system job must stay inert.
