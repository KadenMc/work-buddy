---
name: Running Notes
kind: capability
description: Read native Running Note records from the Journal database. Supports a logical day, date range, recent N-day window, or same-day view.
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
    description: 'Logical Journal date (YYYY-MM-DD). Default: the active Journal day.'
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
requires: []
---

Returns only native records whose item kind is `running_note`, in descending
logical-day order for multi-day queries. It does not enumerate the legacy
Journal tree or infer notes from Markdown headings.
