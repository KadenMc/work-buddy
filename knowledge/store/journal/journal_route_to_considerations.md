---
name: Journal Route To Considerations
kind: capability
description: Walk a journal-group thread's context items and create one consideration note per item. Each item's label becomes the title; raw text becomes the body.
capability_name: journal_route_to_considerations
category: journal
op: op.wb.journal_route_to_considerations
schema_version: wb-capability/v1
parameters:
  thread_id:
    type: str
    description: Group sub-thread to route
    required: true
  vault_root:
    type: str
    description: Override the configured vault root
    required: false
  project:
    type: str
    description: Project slug for all new considerations (default 'inbox')
    required: false
  type:
    type: str
    description: Consideration type (default 'consideration')
    required: false
  status:
    type: str
    description: Initial status (default 'open')
    required: false
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: low
  regret_potential: low
tags:
- journal
- route
- to
- considerations
aliases:
- create considerations from journal group
- route group to considerations
parents:
- journal
requires:
- obsidian
---
