---
name: Setup Help
kind: capability
description: Diagnose why a component isn't working. Runs automated check sequences that walk dependency chains and stop at the first failure with a root cause and fix suggestion. Use 'all' for an overview of all components, or specify a component ID (e.g. 'hindsight', 'obsidian', 'postgresql') for targeted diagnostics.
capability_name: setup_help
category: status
op: op.wb.setup_help
schema_version: wb-capability/v1
parameters:
  component:
    type: str
    description: 'Component ID to diagnose, or ''all'' for overview. Available: chrome_extension, dashboard, datacore, embedding, github_backups, google_calendar, hindsight, lmstudio, messaging, obsidian, postgresql, sidecar, smart_connections, tailscale, telegram, thunderbird'
    required: false
tags:
- status
- setup
- help
aliases:
- diagnose
- troubleshoot
- debug
- why not working
- fix
- health check
- what's wrong
parents:
- status
---
