---
name: Running Notes
kind: capability
description: Read the Running Notes section from the user's daily journal. This is the primary stream-of-consciousness capture zone where the user records ideas, observations, and notes throughout the day. Supports filtering by date range, last N days, or same-day only. Call with same_day=true for just today's entries, or days=N for recent history.
capability_name: running_notes
category: journal
op: op.wb.running_notes
schema_version: wb-capability/v1
parameters:
  same_day:
    type: bool
    description: Only notes from the journal's own date (no carried-over content)
    required: false
  days:
    type: int
    description: Most recent N days (today=1). Cannot combine with start/stop.
    required: false
  start:
    type: str
    description: Include notes from this date onward (YYYY-MM-DD, inclusive)
    required: false
  stop:
    type: str
    description: Include notes up to this date (YYYY-MM-DD, inclusive)
    required: false
  journal_date:
    type: str
    description: 'Journal file date (YYYY-MM-DD). Default: today.'
    required: false
tags:
- journal
- running
- notes
aliases:
- journal notes today
- read daily notes
- user's recent thoughts and observations
- stream of consciousness capture
- journal running notes content
parents:
- journal
requires:
- obsidian
---
