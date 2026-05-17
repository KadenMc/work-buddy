---
schedule: "*/10 * * * *"  # every 10 minutes (dropped from 30 to reduce dashboard-vs-store lag)
jitter_seconds: 90         # spread fire time up to 90s so it doesn't
                           # collide with project-sync on shared ticks
recurring: true
type: capability
capability: task_sync
params: {}
---
Compare the master task list (tasks/master-task-list.md) against the SQLite
metadata store. Auto-creates store records for tasks that were manually added
to the file, flags store orphans (tasks deleted from file), and reports
checkbox state mismatches (file vs store disagree on done/todo).
