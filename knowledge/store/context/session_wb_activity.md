---
name: Session Wb Activity
kind: skill
description: Summary of what a session did through work-buddy's MCP gateway — skills invoked, workflows run, errors, key artifacts. Reads from the per-session activity ledger.
category: context
op: op.wb.session_wb_activity
schema_version: wb-skill/v1
parameters:
  session_id:
    type: str
    description: 'Session ID to query. Default: current session.'
    required: false
skill_name: session_wb_activity
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
