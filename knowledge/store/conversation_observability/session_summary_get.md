---
name: Session Summary Get
kind: capability
description: Look up the cached tldr + topic summaries for one session_id. Returns None when the session hasn't been summarized. Canonical replacement for the verbose `conversation_observability_summary_get`.
capability_name: session_summary_get
category: conversation_observability
op: op.wb.session_summary_get
schema_version: wb-capability/v1
parameters:
  session_id:
    type: str
    description: Full or 8-char prefix session UUID.
    required: true
tags:
- conversation_observability
- conversation
- session
- summary
- get
aliases:
- session summary lookup
- get session tldr
- topic summary for session
- session row
parents:
- conversation_observability
---

Look up the cached legacy session-summary row for one session. The canonical short-name capability for what was previously `conversation_observability_summary_get` (still works as a deprecated alias).

Returns:

```
{
  "session_id": str,
  "tldr": str,
  "topic_count": int,
  "generated_at": str | None,
  "model": str | None,
  "profile": str | None,
  "backend": str | None,
  "prompt_version": int | None,
  "summary_schema_version": int | None,
  "selection_version": int | None,
  "cache_version": int | None,
  "status": str | None,
  "error": str | None,
  "topics": [
    {
      "id": "{session_id}:{i}",
      "session_id": str,
      "topic_index": int,
      "title": str,
      "summary": str,
      "span_start": int | None,
      "span_end": int | None,
      "turn_start": int | None,
      "turn_end": int | None,
      "keywords": list[str],
    },
    ...
  ],
}
```

Returns `None` if the session has no summary.

## When to reach for this vs. related tools

- Use **`session_summary_get`** when you need the legacy row shape (specifically the per-topic `span_start`/`span_end`/`keywords` + the item-level provenance fields like `prompt_version`). The dashboard `/api/chats/<sid>/topics` endpoint and the `claude_session_summary` context collector both consume this shape.
- Use **`drill_tree(domain="summary", node_id=f"conversation_session:{sid}", ...)`** when you want to *navigate* a session's summary tree (index → summary → full depths). That's the agent-facing primitive; `session_summary_get` is the row-shape transform on top of the same store.
- Use **`find(source="summary", scope="conversation_session", drill=true)`** (or its alias **`summary_search`**) when you have a topic and want to *search* across sessions.
