---
name: Session Uncommitted
kind: skill
description: 'Find agent sessions that wrote files still present in dirty git state. Answers: ''which sessions wrote code that was never committed?'' Cross-references Write/Edit/NotebookEdit tool calls against git status --porcelain across all repos.'
category: context
op: op.wb.session_uncommitted
schema_version: wb-skill/v1
parameters:
  days:
    type: int
    description: Lookback window for scanning sessions (default 7)
    required: false
skill_name: session_uncommitted
tags:
- context
- session
- uncommitted
aliases:
- who didn't commit
- uncommitted agent writes
- sessions with dirty files
- what did the agent write but not commit
- dirty files from sessions
parents:
- context
---
