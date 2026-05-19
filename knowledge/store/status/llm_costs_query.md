---
name: Llm Costs Query
kind: capability
description: 'Aggregate LLM cost / usage across one or both data sources (work-buddy''s per-call internal log + Claude Code transcripts). Smart parameters: time window (named or ISO range), group_by (project, model, session, day, tool), source filter, min_cost / project / model filters, and previous-window comparison. Single capability covering most cost questions.'
capability_name: llm_costs_query
category: llm
op: op.wb.llm_costs_query
schema_version: wb-capability/v1
parameters:
  window:
    type: str
    description: 'Time window. Named: ''today'', ''yesterday'', ''7d'', ''30d'', ''90d'', ''month_to_date'', ''all''. ISO range: ''YYYY-MM-DD..YYYY-MM-DD''. Single day: ''YYYY-MM-DD''. Default ''30d''.'
    required: false
  group_by:
    type: str
    description: 'Optional breakdown: ''project'', ''model'', ''session'', ''day'', ''tool''. Omit for top-line totals only.'
    required: false
  source:
    type: str
    description: '''all'' (default), ''internal'' (work-buddy''s runner calls), or ''claude_code'' (Claude Code transcripts).'
    required: false
  min_cost:
    type: float
    description: Filter rows where cost_usd < this. Default 0.
    required: false
  project:
    type: str
    description: Substring match on project name / cwd.
    required: false
  model:
    type: str
    description: Exact-match model filter (e.g. 'claude-sonnet-4-6').
    required: false
  top_n:
    type: int
    description: Cap on grouped rows. Default 10. Pass 0 for no cap.
    required: false
  include_local:
    type: bool
    description: Include local-LLM rows? Default true.
    required: false
  compare_to_previous:
    type: bool
    description: Include previous-equivalent-window comparison (delta_pct_cost, delta_pct_calls). Default true.
    required: false
tags:
- llm
- costs
- query
aliases:
- llm costs
- llm spend
- how much have i spent
- claude api costs
- cost by project
- cost by model
- weekly spend
- monthly spend
- expensive sessions
- burn rate
- cost trend
- spend comparison
parents:
- status
---
