---
name: Sidecar Jobs
kind: capability
description: List all scheduled sidecar jobs with their next fire time, heartbeat status, and whether exclusion windows are active.
capability_name: sidecar_jobs
category: status
op: op.wb.sidecar_jobs
schema_version: wb-capability/v1
tags:
- status
- sidecar
- jobs
aliases:
- cron
- scheduled jobs
- heartbeat
- sidecar schedule
parents:
- status
---
