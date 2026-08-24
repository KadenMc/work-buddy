---
schedule: "*/10 * * * *"  # every 10 minutes (dropped from 30 to reduce dashboard-vs-store lag)
jitter_seconds: 90         # spread fire time up to 90s so it doesn't
                           # collide with project-sync on shared ticks
recurring: true
enabled: false
type: capability
capability: task_sync
params: {}
---
Retired legacy reconciliation job. It remains disabled indefinitely after the
native task-authority cutover so no scheduled process reads from, writes to, or
attempts to reconcile the frozen Obsidian task files.
