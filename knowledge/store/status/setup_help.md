---
name: Setup Help
kind: capability
description: Diagnose wanted components. An opted-out component or a child of an opted-out dependency returns disabled without probes, requirement checks, retries, or setup advice. Use 'all' for an overview or provide a component ID.
capability_name: setup_help
category: status
op: op.wb.setup_help
schema_version: wb-capability/v1
parameters:
  component:
    type: str
    description: 'Component ID to diagnose, or ''all'' for overview. google_calendar_native is the supported Calendar owner; google_calendar, datacore, and obsidian are explicit legacy compatibility components.'
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
