---
name: Session Resume
kind: capability
description: Resume an existing Claude Code session in a new local terminal window. No prompt is sent and remote-control is off — the terminal opens directly into the conversation, ready for the user to type. cwd is auto-derived from the session's recorded working directory.
capability_name: session_resume
category: sidecar
parameters:
  session_id:
    type: str
    description: Full or partial (>=8-char prefix) session UUID to resume.
    required: true
  cwd:
    type: str
    description: Override the working directory. Defaults to the session's own recorded cwd, falling back to repo root.
    required: false
  bypass_permissions:
    type: bool
    description: 'Add --dangerously-skip-permissions. Default: True.'
    required: false
mutates_state: true
retry_policy: manual
tags:
- sidecar
- session
- resume
aliases:
- resume session
- continue session
- reopen chat
- pick up conversation
- claude --resume
- reattach
- open terminal to session
parents:
- status
- status
---
