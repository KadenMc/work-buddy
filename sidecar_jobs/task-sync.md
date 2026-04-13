---
schedule: "*/30 * * * *"  # every 30 minutes
recurring: true
type: capability
capability: task_sync
params: {}
---
Compare the master task list (tasks/master-task-list.md) against the SQLite
metadata store. Auto-creates store records for tasks that were manually added
to the file, flags store orphans (tasks deleted from file), and reports
checkbox state mismatches (file vs store disagree on done/todo).
