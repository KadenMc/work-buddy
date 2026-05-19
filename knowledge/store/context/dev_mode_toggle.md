---
name: Dev Mode Toggle
kind: capability
description: Toggle dev mode for the current session. When active, all knowledge queries automatically include dev_notes — development-facing documentation that operational agents don't need. Use True to enable, False to disable, or omit to toggle.
capability_name: dev_mode_toggle
category: context
op: op.wb.dev_mode_toggle
schema_version: wb-capability/v1
parameters:
  enabled:
    type: bool
    description: True=on, False=off, omit=toggle current state.
    required: false
tags:
- context
- dev
- mode
- toggle
aliases:
- dev mode
- developer mode
- development mode
- toggle dev
- enable dev notes
parents:
- context
---
