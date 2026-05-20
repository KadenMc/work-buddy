---
name: Session Commits
kind: capability
description: Extract git commits made during Claude Code sessions. Parses raw JSONL for Bash tool calls containing 'git commit' and their results. Scope to one session or scan all recent sessions.
capability_name: session_commits
category: context
op: op.wb.session_commits
schema_version: wb-capability/v1
parameters:
  session_id:
    type: str
    description: Full or partial session UUID. If omitted, scans all recent sessions.
    required: false
  days:
    type: int
    description: Lookback window when scanning all sessions (default 7)
    required: false
tags:
- context
- session
- commits
aliases:
- commits in session
- what was committed
- git commits from conversation
- session git history
- what did the agent commit
parents:
- context
---
