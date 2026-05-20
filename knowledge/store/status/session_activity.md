---
name: Session Activity
kind: capability
description: Query the session activity ledger — what this agent session has done through work-buddy. Filters by event type, capability, category, status. Returns last N matching entries (newest first).
capability_name: session_activity
category: status
op: op.wb.session_activity
schema_version: wb-capability/v1
parameters:
  event_type:
    type: str
    description: 'Filter: capability_invoked, workflow_started, workflow_step_completed, search_performed'
    required: false
  capability_name:
    type: str
    description: Filter to a specific capability name
    required: false
  category:
    type: str
    description: Filter by category (tasks, journal, context, etc.)
    required: false
  status:
    type: str
    description: 'Filter by status: ok, error, consent_required'
    required: false
  last_n:
    type: int
    description: Return last N matching entries (default 20)
    required: false
  include_searches:
    type: bool
    description: Include wb_search events (default false)
    required: false
tags:
- status
- session
- activity
aliases:
- what did I do
- session history
- activity log
- what happened
- session activity
- ledger
parents:
- status
---
