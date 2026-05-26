---
name: Conversation Observability Summary Get
kind: capability
description: 'DEPRECATED ALIAS — use `session_summary_get` instead. Same callable, shorter canonical name. Look up the cached tldr + topic summaries for one session_id; returns None when nothing has been summarized yet.'
capability_name: conversation_observability_summary_get
category: conversation_observability
op: op.wb.conversation_observability_summary_get
schema_version: wb-capability/v1
parameters:
  session_id:
    type: str
    description: Full or 8-char prefix session UUID.
    required: true
tags:
- conversation_observability
- conversation
- observability
- summary
- get
- deprecated
aliases:
- session summary lookup
- get session tldr
- topic summary for session
parents:
- conversation_observability
---

**Alias of [`session_summary_get`](session_summary_get).** Both names bind to the same Python callable (`session_summary_row`); the long namespace prefix is preserved for back-compat. Reach for `session_summary_get` in new code.

Returns the row dict: `{session_id, tldr, topic_count, generated_at, model, profile, backend, prompt_version, summary_schema_version, selection_version, cache_version, status, error, topics: [...]}`. For the topic-tree alone, an agent can also use `drill_tree(domain="summary", node_id=f"conversation_session:{sid}", depth="summary")` — that's the canonical agent-facing path for navigating summary trees.
