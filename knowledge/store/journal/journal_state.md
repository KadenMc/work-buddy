---
name: Journal State
kind: capability
description: 'Read the native Journal day: logical date, activity window, frozen profile, typed fields, and entries.'
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
    description: When true, persist the resolved logical day and its immutable profile snapshot. Default false; an empty day is otherwise read without mutation.
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
requires: []
---

Read Journal state from the Journal SQLite authority for an explicit date or the active logical Journal day. `target="today"` is resolved by the backend under the configured Journal timezone and boundary; it does not mean the browser's civil date.

The result preserves the established activity-window fields and also returns the frozen profile identity, composition digest, native items, typed field values, and authority state. Consumers use the returned window for activity and calendar queries rather than adding 24 hours or reconstructing timezone arithmetic.

`create_on_read` persists only the logical-day composition. It never creates or reads a Markdown file. A missing day remains a read-only `exists=false` result unless persistence was explicitly requested.

See `journal/day-lifecycle` for DST and policy-transition semantics.
