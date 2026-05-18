---
name: Llm With Tools
kind: capability
description: 'Invoke a local model with restricted work-buddy MCP tool access, so it can look things up (projects, tasks, journal, context) while answering. Tool access is limited to a named preset defined in work_buddy/llm/tool_presets.py (currently: ''readonly_safe'', ''readonly_context''). No arbitrary tool list accepted at call time — presets are the security boundary. Requires ''profile'' and ''tool_preset''.'
capability_name: llm_with_tools
category: llm
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
- status
---
