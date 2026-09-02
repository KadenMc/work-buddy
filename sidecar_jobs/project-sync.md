---
schedule: "*/30 * * * *"  # every 30 minutes
jitter_seconds: 240        # spread fire time up to 4 min so it doesn't
                           # collide with task-sync on shared :00/:30 ticks
recurring: true
type: capability
capability: project_sync
params: {}
enabled: false
---
Retired legacy reconciliation for project Markdown notes.

The registered `project_sync` capability is authority-aware through
`reconcile_projects_authoritatively`. It performs the legacy reconciliation
only before the live Projects seal and deterministically returns
`status=disabled`, `reason=projects_sqlite_authority`, with zero writes after
that seal.

An operator may invoke one final explicit reconciliation before staging the
cutover cohort. The recurring job stays disabled so it cannot race the final
source hash, fence, or seal, and its presence never authorizes a post-seal file
read or write.
