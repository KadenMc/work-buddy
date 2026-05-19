---
name: Session Uncommitted
kind: capability
description: 'Find agent sessions that wrote files still present in dirty git state. Answers: ''which sessions wrote code that was never committed?'' Cross-references Write/Edit/NotebookEdit tool calls against git status --porcelain across all repos.'
capability_name: session_uncommitted
category: context
op: op.wb.session_uncommitted
schema_version: wb-capability/v1
parameters:
  days:
    type: int
    description: Lookback window for scanning sessions (default 7)
    required: false
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
