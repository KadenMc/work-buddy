---
name: Context Chat
kind: capability
description: Recent Claude Code conversations and CLI history with tool usage, duration, and outcome snippets
capability_name: context_chat
category: context
op: op.wb.context_chat
schema_version: wb-capability/v1
parameters:
  days:
    type: int
    description: Lookback window (default 7)
    required: false
  last:
    type: int
    description: Cap sessions returned per source
    required: false
param_aliases:
  since: days
tags:
- context
- chat
aliases:
- recent conversations
- claude code sessions
- what sessions happened
- chat history
- agent sessions
- conversation log
parents:
- context
---
