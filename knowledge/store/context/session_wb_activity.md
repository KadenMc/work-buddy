---
name: Session Wb Activity
kind: capability
description: Summary of what a session did through work-buddy's MCP gateway — capabilities invoked, workflows run, errors, key artifacts. Reads from the per-session activity ledger.
capability_name: session_wb_activity
category: context
op: op.wb.session_wb_activity
schema_version: wb-capability/v1
parameters:
  session_id:
    type: str
    description: 'Session ID to query. Default: current session.'
    required: false
tags:
- context
- session
- wb
- activity
aliases:
- what did the session do
- session work-buddy activity
- mcp activity
- gateway activity
parents:
- context
---
