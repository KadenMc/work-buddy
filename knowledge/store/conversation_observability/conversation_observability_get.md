---
name: Conversation Observability Get
kind: capability
description: 'One session''s full picture, composed. Returns the observed-session row (metadata: start/end, message_count, span/tool counts) and, via opt-in flags, its LLM summary, commits, file writes, and PR activity — one call instead of four. All flags default off, so a bare call returns just the observed row. Complements session_summary_get (the LLM summary alone, with its own generation status). Returns None when the session has not been observed yet.'
capability_name: conversation_observability_get
category: conversation_observability
op: op.wb.conversation_observability_get
schema_version: wb-capability/v1
parameters:
  session_id:
    type: str
    description: Full or 8-char prefix session UUID.
    required: true
  include_summary:
    type: bool
    description: Add the LLM tldr under a "summary" key (its own status/error reflects summary-generation health, distinct from the row's parse health).
    required: false
  include_commits:
    type: bool
    description: Add the session's commits under a "commits" key.
    required: false
  include_writes:
    type: bool
    description: Add the session's file writes under a "writes" key.
    required: false
  include_prs:
    type: bool
    description: Add the session's GitHub PR events under a "prs" key.
    required: false
  include_topics:
    type: bool
    description: Keep the summary's per-topic timeline (implies include_summary); omitted otherwise to stay compact.
    required: false
tags:
- conversation_observability
- conversation
- observability
- get
aliases:
- session observability lookup
- session metadata get
- agent session details
- tell me about this session
parents:
- conversation_observability
---
