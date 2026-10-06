---
name: Sidecar Status
kind: skill
description: 'Check if the sidecar daemon is running and get its current state: supervised services health, scheduler status, upcoming job schedule, and the runtime context the daemon recorded (its interpreter, its console, and what its services run on).'
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
