---
name: Remote Session Begin
kind: capability
description: Launch or resume a visible Claude Code session in a real terminal window. If session_id or session_name is provided, resumes that session; otherwise starts a new one. Designed for Remote Control (phone app) connection.
capability_name: remote_session_begin
category: sidecar
op: op.wb.remote_session_begin
schema_version: wb-capability/v1
parameters:
  cwd:
    type: str
    description: Working directory. Defaults to repo root.
    required: false
  prompt:
    type: str
    description: Initial prompt for a new session. Ignored when resuming.
    required: false
  session_id:
    type: str
    description: Session ID to resume. Triggers resume mode.
    required: false
  session_name:
    type: str
    description: Session name to look up for resume. Triggers resume mode.
    required: false
  bypass_permissions:
    type: bool
    description: 'Add --dangerously-skip-permissions so the session operates without interactive permission prompts. Default: True.'
    required: false
mutates_state: true
retry_policy: manual
tags:
- sidecar
- remote
- session
- begin
aliases:
- remote session
- launch terminal
- start claude
- remote control
- visible session
- phone session
- telegram
- remote launch
- resume session
- continue session
- reconnect
parents:
- status
---
