---
name: Remote Session List
kind: capability
description: List resumable Claude Code sessions from ~/.claude/sessions/. Shows session ID, name, cwd, and start time.
capability_name: remote_session_list
category: sidecar
op: op.wb.remote_session_list
schema_version: wb-capability/v1
parameters:
  cwd:
    type: str
    description: Filter to sessions started in this directory. Defaults to repo root.
    required: false
tags:
- sidecar
- remote
- session
- list
aliases:
- list sessions
- resumable sessions
- session picker
- active sessions
- find session
parents:
- status
---
