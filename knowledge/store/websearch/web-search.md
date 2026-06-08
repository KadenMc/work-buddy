---
name: Web Search
kind: capability
description: General web search — Jina default with a keyless ddgs fallback. Returns ranked hits (title, url, snippet, and full page text when the backend provides it). Ephemeral — results are not persisted. Use for arbitrary lookups, research, or fact-checking from inside an agent flow.
capability_name: web_search
category: websearch
op: op.wb.web_search
schema_version: wb-capability/v1
parameters:
  query:
    type: str
    description: The search query.
    required: true
  max_results:
    type: int
    description: Maximum hits to return (default 8).
    required: false
  topic:
    type: str
    description: Optional topic hint, e.g. "news".
    required: false
  time_range:
    type: str
    description: Optional recency filter — d|w|m|y or a custom date range.
    required: false
tags:
- websearch
- search
- retrieval
aliases:
- web search
- search the web
- look something up online
- google it
- find on the internet
parents:
- websearch
requires: []
---
