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
