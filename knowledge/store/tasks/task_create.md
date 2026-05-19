---
name: Task Create
kind: capability
description: Create a new task in the master task list. Optionally attach a note file for details/subtasks. Slice 2 GTD vocabulary (task_kind, density, outcome_text, next_action_text, definition_of_done, creation_effort, user_involvement, creation_provenance, deadline, dependency) is optional and defaults to 'looks like a legacy manually-authored task'. Agent-driven creators should set creation_provenance (e.g. 'agent_inferred_from_journal') and lower user_involvement.
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
    description: 'Project slug (added as #projects/<slug>)'
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
    description: If provided, creates a linked note file with this summary
    required: false
  tags:
    type: list[str]
    description: Namespace tags (no leading '#'), e.g. ['paper/ecg-classifier', 'experiment/augmentation']. Appended to the task line; picked up into the tag cache on next task_sync.
    required: false
  task_kind:
    type: str
    description: 'Slice 2: ''task'' (default) | ''periodic'' | ''habit''.'
    required: false
  density:
    type: str
    description: 'Slice 2: ''sparse'' (default) | ''developed''. (''dense'' is forward-compat for Slice 7+.)'
    required: false
  outcome_text:
    type: str
    description: 'Slice 2: desired end-state for developed tasks (e.g. ''ETF tracking habit running'').'
    required: false
  next_action_text:
    type: str
    description: 'Slice 2: specific physical action for developed tasks (e.g. ''Set up weekly cron job'').'
    required: false
  definition_of_done:
    type: str
    description: 'Slice 2: closing signal for the task.'
    required: false
  creation_effort:
    type: str
    description: 'Slice 2: ''sparse'' | ''medium'' | ''developed'' (default — assumes manual creation).'
    required: false
  user_involvement:
    type: str
    description: 'Slice 2: ''low'' | ''medium'' | ''high'' (default — assumes manual creation).'
    required: false
  creation_provenance:
    type: str
    description: 'Slice 2: ''manual'' (default) | ''agent_inferred_from_journal'' | ''agent_inferred_from_chrome'' | ''agent_inferred_from_inline'' | other.'
    required: false
  has_deadline:
    type: bool
    description: 'Slice 2: True when deadline_date is set; signal for deadline-aware resurfacing in Slice 8.'
    required: false
  deadline_date:
    type: str
    description: 'Slice 2: ISO date YYYY-MM-DD when has_deadline=True.'
    required: false
  has_dependency:
    type: bool
    description: 'Slice 2: True when this task is blocked on someone or something.'
    required: false
  dependency_hint:
    type: str
    description: 'Slice 2: free-text hint about the dependency (e.g. ''needs Ben’s review'').'
    required: false
mutates_state: true
retry_policy: verify_first
consent_operations:
- tasks.create_task
- obsidian.write_file
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
requires:
- obsidian
---
