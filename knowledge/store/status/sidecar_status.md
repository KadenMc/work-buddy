---
name: Sidecar Status
kind: capability
description: 'Check if the sidecar daemon is running and get its current state: supervised services health, scheduler status, and upcoming job schedule.'
capability_name: sidecar_status
category: status
op: op.wb.sidecar_status
schema_version: wb-capability/v1
tags:
- status
- sidecar
aliases:
- daemon
- sidecar
- process supervisor
- services health
parents:
- status
---
