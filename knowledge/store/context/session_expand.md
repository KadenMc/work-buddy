---
name: Session Expand
kind: capability
description: Full context around a specific message in a session. Returns untruncated text for the target and surrounding messages.
capability_name: session_expand
category: context
parameters:
  session_id:
    type: str
    description: Full or partial (8-char) session UUID
    required: true
  message_index:
    type: int
    description: Zero-based message index
    required: true
  context_window:
    type: int
    description: Messages before+after to include (default 5)
    required: false
tags:
- context
- session
- expand
aliases:
- expand message
- message context
- surrounding messages
- zoom into a message
- show context window
- what came before this message
- conversation context around message
parents:
- context
- context
---
