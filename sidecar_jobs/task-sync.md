---
schedule: "*/10 * * * *"  # every 10 minutes (F2: dropped from 30 to reduce dashboard-vs-store lag)
recurring: true
type: capability
capability: task_sync
params: {}
---
Compare the master task list (tasks/master-task-list.md) against the SQLite
metadata store. Auto-creates store records for tasks that were manually added
to the file, flags store orphans (tasks deleted from file), and reports
checkbox state mismatches (file vs store disagree on done/todo).
