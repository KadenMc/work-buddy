---
name: Workflow Create
kind: capability
description: Create a new workflow unit (DAG + step instructions). Use this instead of docs_create for kind='workflow' units — docs_create does not accept workflow-specific fields.
capability_name: workflow_create
category: context
parameters:
  path:
    type: str
    description: Unique path ID (e.g. 'dev/dev-document')
    required: true
  name:
    type: str
    description: Human-readable name
    required: true
  description:
    type: str
    description: One-line summary
    required: true
  workflow_name:
    type: str
    description: Registry slug used with wb_run('<workflow_name>')
    required: true
  steps:
    type: str
    description: 'JSON array of step dicts. Each step requires at least id, name, step_type (''reasoning'' or ''code''), and depends_on (list of prior step ids). Additional keys: auto_run, visibility, result_schema, invokes, optional.'
    required: true
  step_instructions:
    type: str
    description: JSON object mapping step_id -> instruction text. Reasoning steps generally need this; pure auto_run steps usually don't.
    required: false
  execution:
    type: str
    description: 'Default execution policy: ''main'' or ''subagent'' (default ''main'')'
    required: false
  allow_override:
    type: bool
    description: Allow per-step execution override (default false)
    required: false
  content_full:
    type: str
    description: Workflow-level context (philosophy, what-not-to-do). Surfaces at depth='full'.
    required: false
  content_summary:
    type: str
    description: One-paragraph summary.
    required: false
  command:
    type: str
    description: Slash command name (e.g. 'wb-dev-document')
    required: false
  parents:
    type: str
    description: 'Comma-separated parent paths (typical: domain, e.g. ''dev'')'
    required: false
  children:
    type: str
    description: Comma-separated child paths
    required: false
  tags:
    type: str
    description: Comma-separated search tags
    required: false
  aliases:
    type: str
    description: Comma-separated search aliases
    required: false
  dev_notes:
    type: str
    description: Dev-mode-only notes about the workflow's internals
    required: false
  params_schema:
    type: str
    description: 'Optional JSON object declaring caller-provided initial params: {param_name: {type, description, required}}. Mirrors capability parameters. Workflows that omit this reject any non-empty params at start.'
    required: false
mutates_state: true
retry_policy: manual
tags:
- context
- workflow
- create
aliases:
- create workflow
- new workflow
- author workflow DAG
- register workflow
- add workflow unit
- define workflow
parents:
- context
- context
---
