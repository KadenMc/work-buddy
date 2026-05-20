---
name: Inline Sync
kind: capability
description: 'Reconcile vault #wb/cmd/* tags with the persistent watcher store.'
capability_name: inline_sync
category: inline
op: op.wb.inline_sync
schema_version: wb-capability/v1
mutates_state: true
retry_policy: manual
tags:
- inline
- sync
aliases:
- sync inline watchers
- reconcile wb/cmd tags
- inline watcher drift
parents:
- inline
---
