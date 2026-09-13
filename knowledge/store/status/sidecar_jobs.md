---
name: Sidecar Jobs
kind: skill
description: List all sidecar jobs with their enabled state, next eligible fire time, heartbeat status, and whether exclusion windows are active. Disabled jobs report no next fire.
category: status
op: op.wb.sidecar_jobs
schema_version: wb-skill/v1
skill_name: sidecar_jobs
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
