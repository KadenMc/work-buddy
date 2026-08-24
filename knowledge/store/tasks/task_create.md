---
name: Task Create
kind: capability
description: Create a native task and optionally provision a projection-free Co-work knowledge document. GTD vocabulary is optional; agent-driven creators should set creation_provenance and appropriate user_involvement.
capability_name: task_create
category: tasks
op: op.wb.task_create
schema_version: wb-capability/v1
parameters:
  task_text:
    type: str
    description: Short single-line task description (NO newlines — will be rejected)
    required: true
  urgency:
    type: str
    description: 'Urgency: low, medium (default), high'
    required: false
  project:
    type: str
    description: Project slug stored as a structured project tag.
    required: false
  due_date:
    type: str
    description: Due date as YYYY-MM-DD
    required: false
  contract:
    type: str
    description: Contract slug this task serves
    required: false
  summary:
    type: str
    description: If provided, creates a linked Co-work knowledge document with this initial content.
    required: false
  tags:
    type: list[str]
    description: Structured namespace tags without a leading '#'.
    required: false
  task_kind:
    type: str
    description: '''task'' (default) | ''periodic'' | ''habit''.'
    required: false
  density:
    type: str
    description: '''sparse'' (default) | ''developed''. (''dense'' is forward-compat.)'
    required: false
  outcome_text:
    type: str
    description: 'desired end-state for developed tasks (e.g. ''ETF tracking habit running'').'
    required: false
  next_action_text:
    type: str
    description: 'specific physical action for developed tasks (e.g. ''Set up weekly cron job'').'
    required: false
  definition_of_done:
    type: str
    description: 'closing signal for the task.'
    required: false
  creation_effort:
    type: str
    description: '''sparse'' | ''medium'' | ''developed'' (default, assumes manual creation).'
    required: false
  user_involvement:
    type: str
    description: '''low'' | ''medium'' | ''high'' (default, assumes manual creation).'
    required: false
  creation_provenance:
    type: str
    description: '''manual'' (default) | ''agent_inferred_from_journal'' | ''agent_inferred_from_chrome'' | ''agent_inferred_from_inline'' | other.'
    required: false
  has_deadline:
    type: bool
    description: 'True when deadline_date is set; signal for deadline-aware resurfacing.'
    required: false
  deadline_date:
    type: str
    description: 'ISO date YYYY-MM-DD when has_deadline=True.'
    required: false
  has_dependency:
    type: bool
    description: 'True when this task is blocked on someone or something.'
    required: false
  dependency_hint:
    type: str
    description: 'free-text hint about the dependency (e.g. ''needs Ben’s review'').'
    required: false
  client_mutation_id:
    type: str
    description: Optional stable idempotency key. The gateway pins one before dispatch when omitted.
    required: false
mutates_state: true
retry_policy: verify_first
consent_operations:
- tasks.create_task
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
tags:
- tasks
- task
- create
aliases:
- new task
- add task
- create todo
- add todo
parents:
- tasks
requires: []
---

The creating agent session is recorded automatically as `created_by_session`; it is not a caller parameter. The result includes the native task ID, task and collection revisions, a durable mutation receipt, and Co-work document metadata when `summary` provisions knowledge. It never returns a Markdown task line or note path. Replaying the same `client_mutation_id` after response loss returns the original semantic result.
