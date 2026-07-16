---
name: Journal State
kind: capability
description: 'Read journal state: target date, activity window, existing entries'
capability_name: journal_state
category: journal
op: op.wb.journal_state
schema_version: wb-capability/v1
parameters:
  target:
    type: str
    description: 'Date target: ''today'', ''yesterday'', or YYYY-MM-DD'
    required: false
  create_on_read:
    type: bool
    description: When true, ensure the journal exists (creates today's note via Obsidian if missing). Default false - a missing journal reads as exists=false with no file created. Creation is only possible for today.
    required: false
param_aliases:
  target_date: target
  date: target
tags:
- journal
- state
aliases:
- journal status
- today's journal state
- journal target date
- what's in today's journal
- activity window
- current journal entries
- journal metadata
parents:
- journal
requires:
- obsidian
---

Read Journal state for an explicit date or the active logical Journal day. `target="today"` is resolved by the backend under the configured Journal timezone and boundary; it does not mean the browser's civil date.

The result includes the resolved logical date, timezone, boundary, aware window start/end, and policy metadata alongside journal existence and content. Consumers use that window for activity and calendar queries rather than adding 24 hours or reconstructing timezone arithmetic.

`create_on_read` may create only the active logical day's note. A missing note remains a read-only `exists=false` result unless creation was explicitly requested.

See `journal/day-lifecycle` for DST and policy-transition semantics.
