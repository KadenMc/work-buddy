---
name: Activity Timeline
kind: capability
description: Infer recent activity from journal entries and optionally deeper signals. Returns a structured timeline with events, gaps, and relative timestamps. Use for understanding what happened during a time window.
capability_name: activity_timeline
category: journal
parameters:
  since:
    type: str
    description: ISO datetime or relative shorthand (e.g. '2h', '1d', '30m')
    required: true
  until:
    type: str
    description: 'ISO datetime. Default: now.'
    required: false
  deep:
    type: bool
    description: 'Also collect git/chat/vault signals (default: false)'
    required: false
  target_date:
    type: str
    description: 'Journal date YYYY-MM-DD (default: inferred from since)'
    required: false
tags:
- journal
- activity
- timeline
aliases:
- what happened recently
- recent activity
- activity timeline
- what have I been doing
- infer activity
- activity digest
- journal entries structured
parents:
- journal
- journal
requires:
- obsidian
---
