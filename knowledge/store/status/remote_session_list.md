---
name: Remote Session List
kind: skill
description: List resumable Claude Code sessions from ~/.claude/sessions/. Shows session ID, name, cwd, and start time.
category: sidecar
op: op.wb.remote_session_list
schema_version: wb-skill/v1
parameters:
  cwd:
    type: str
    description: Filter to sessions started in this directory. Defaults to repo root.
    required: false
skill_name: remote_session_list
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
