---
name: Journal State
kind: capability
description: 'Read journal state: target date, activity window, existing entries'
capability_name: journal_state
category: journal
parameters:
  target:
    type: str
    description: 'Date target: ''today'', ''yesterday'', or YYYY-MM-DD'
    required: false
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
- journal
requires:
- obsidian
---
