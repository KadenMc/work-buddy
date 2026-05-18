---
name: Collect And Orient
kind: workflow
description: Generate a fresh context bundle and use it to orient on the user's current work state. This is the primary "what's going on right now?" workflow.
workflow_name: collect-and-orient
execution: main
steps:
- id: run-collector
  name: Run the context bundle collector
  step_type: code
  depends_on: []
  invokes: []
- id: read-bundle
  name: Read the context bundle files in priority order
  step_type: code
  depends_on:
  - run-collector
  visibility:
    mode: summary
  invokes: []
- id: synthesize
  name: Synthesize a brief orientation
  step_type: reasoning
  depends_on:
  - read-bundle
  invokes: []
- id: connect-contracts
  name: Cross-reference with active contracts
  step_type: reasoning
  depends_on:
  - synthesize
  invokes: []
- id: suggest-action
  name: Suggest one next action
  step_type: reasoning
  depends_on:
  - connect-contracts
  invokes: []
tags:
- context
- collect
- and
- orient
parents:
- context
---
