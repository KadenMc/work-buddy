---
name: Sidecar Status
kind: skill
description: 'Check if the sidecar daemon is running and get its current state: supervised services health, scheduler status, and upcoming job schedule.'
category: status
op: op.wb.sidecar_status
schema_version: wb-skill/v1
skill_name: sidecar_status
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
