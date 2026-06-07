---
name: Llm With Tools
kind: capability
description: 'Invoke a local model with restricted work-buddy MCP tool access, so it can look things up (projects, tasks, journal, context) while answering. Tool access is limited to a named preset defined in work_buddy/llm/tool_presets.py (currently: ''readonly_safe'', ''readonly_context''). No arbitrary tool list accepted at call time — presets are the security boundary. Requires ''profile'' and ''tool_preset''.'
capability_name: llm_with_tools
category: llm
op: op.wb.llm_with_tools
schema_version: wb-capability/v1
parameters:
  system:
    type: str
    description: System prompt (becomes 'instructions' on the native chat request)
    required: true
  user:
    type: str
    description: User query (becomes 'input')
    required: true
  profile:
    type: str
    description: Named local profile (e.g., 'local_general') — must be LM Studio-backed
    required: true
  tool_preset:
    type: str
    description: 'Named whitelist of allowed work-buddy tools. Currently: ''readonly_safe'', ''readonly_context''. Presets are code, not config — defined in work_buddy/llm/tool_presets.py.'
    required: true
  required_capabilities:
    type: list[str]
    description: Optional list of capability names the model MUST be able to call (e.g. ['update-journal', 'journal_write']). Pre-flight checked against the preset; if any are missing, the call fails fast with an explicit error. Use this to catch goal-preset mismatches — e.g. running a workflow from a read-only preset that doesn't include the workflow's name.
    required: false
  previous_response_id:
    type: str
    description: Continue a prior LM Studio stateful-chat turn
    required: false
  max_tokens:
    type: int
    description: Output budget. Default 4096 (tool-calling eats tokens).
    required: false
  temperature:
    type: float
    description: Sampling temperature (default 0.0)
    required: false
  store:
    type: bool
    description: Let LM Studio retain this turn server-side (default False)
    required: false
  persist_tool_results:
    type: bool
    description: When True, raw MCP tool outputs are saved to the artifact store and the artifact id is embedded in each tool_calls entry (output_artifact_id). Default False — responses contain only tool-call metadata, not raw output. Errors auto-escalate to persist regardless of this flag.
    required: false
invokes:
- active_contracts
- activity_timeline
- agent_docs
- artifact_get
- artifact_list
- chrome_activity
- chrome_content
- chrome_infer
- context_bundle
- context_calendar
- context_chat
- context_chrome
- context_git
- context_messages
- context_obsidian
- context_projects
- context_search
- context_tasks
- context_vault
- context_wellness
- contract_constraints
- contract_health
- contract_wip_check
- contracts_summary
- conversation_list
- datacore_compile_plan
- datacore_evaluate
- datacore_fullquery
- datacore_get_page
- datacore_query
- datacore_run_plan
- datacore_schema
- datacore_status
- datacore_validate
- day_planner
- feature_status
- get_thread
- hot_files
- ir_index
- journal_state
- knowledge
- knowledge_personal
- list_sessions
- overdue_contracts
- project_get
- project_list
- query_messages
- read_message
- running_notes
- service_health
- session_activity
- session_commits
- session_expand
- session_get
- session_locate
- session_search
- session_summary
- session_uncommitted
- sidecar_jobs
- sidecar_status
- stale_contracts
- tailscale_status
- task_briefing
- task_review_inbox
- task_scattered
- task_stale_check
- weekly_review_data
auto_retry: false
tags:
- llm
- with
- tools
aliases:
- local llm with tools
- llm tool access
- mcp tools local
- contextualize local
- local model tools
- lm studio mcp
- qwen with tools
- tool use local
parents:
- status
---
