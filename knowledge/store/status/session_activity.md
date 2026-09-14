---
name: Session Activity
kind: skill
description: Query the session activity ledger — what this agent session has done through work-buddy. Filters by event type, skill, category, status. Returns last N matching entries (newest first).
category: status
op: op.wb.session_activity
schema_version: wb-skill/v1
parameters:
  event_type:
    type: str
    description: 'Filter: skill_invoked, workflow_started, workflow_step_completed, search_performed'
    required: false
  skill_name:
    type: str
    description: Filter to a specific skill name
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
param_aliases:
  capability_name: skill_name
skill_name: session_activity
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
