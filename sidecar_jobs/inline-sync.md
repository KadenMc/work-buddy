---
schedule: "*/10 * * * *"  # every 10 minutes
recurring: true
type: capability
capability: inline_sync
params: {}
---
Reconcile vault persistent #wb/cmd/* tags against the inline watcher store.
Adds watchers for newly-detected persistent tags, removes watchers whose tag
has disappeared from the vault, and surfaces watchers due to run.
