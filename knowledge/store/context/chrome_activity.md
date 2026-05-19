---
name: Chrome Activity
kind: capability
description: 'Query Chrome browsing history from the rolling tab ledger. Supports: hot_tabs (ranked by engagement), changes (opened/closed/navigated/engaged/moved), sessions (domain clusters), tabs_at (snapshot at a time), context (tab proximity and window layout), details (full URLs by filter), status (ledger health). Output is compact (no URLs) — use details query for full URLs.'
capability_name: chrome_activity
category: context
op: op.wb.chrome_activity
schema_version: wb-capability/v1
parameters:
  query:
    type: str
    description: 'Query type: hot_tabs, changes, sessions, tabs_at, context, details, status (default: hot_tabs)'
    required: false
  since:
    type: str
    description: 'Start of window. Relative (''2h'', ''1d'') or ISO datetime (default: 2h)'
    required: false
  until:
    type: str
    description: 'End of window. Default: now.'
    required: false
  limit:
    type: int
    description: Max results for hot_tabs (default 20)
    required: false
  timestamp:
    type: str
    description: 'For tabs_at/details queries: ISO datetime or relative shorthand'
    required: false
  filter:
    type: str
    description: 'For details query: domain or title substring to match'
    required: false
tags:
- context
- chrome
- activity
aliases:
- browsing history
- what was I browsing
- chrome tab history
- tab activity
- what tabs were open
- browsing sessions
- hot tabs
- browser activity
parents:
- context
requires:
- chrome_extension
---
