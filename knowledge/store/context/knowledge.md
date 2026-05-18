---
name: Knowledge
kind: capability
description: Search across both system documentation and personal knowledge from the Obsidian vault. Returns results tagged with their source scope (system or personal).
capability_name: knowledge
category: context
parameters:
  query:
    type: str
    description: Natural language search.
    required: false
  path:
    type: str
    description: Exact unit path for direct lookup.
    required: false
  scope:
    type: str
    description: Path prefix to filter to a subtree.
    required: false
  kind:
    type: str
    description: 'Filter by kind: directions, capability, workflow, system, service, integration, reference, concept, personal.'
    required: false
  category:
    type: str
    description: 'Filter personal units by category: work_pattern, self_regulation, skill_gap, feedback, preference, reference.'
    required: false
  severity:
    type: str
    description: 'Filter personal units by severity: HIGH, MODERATE, LOW.'
    required: false
  depth:
    type: str
    description: 'Content depth: ''index'', ''summary'' (default), ''full''.'
    required: false
  top_n:
    type: int
    description: Max search results (default 8).
    required: false
  dev:
    type: bool
    description: Include dev_notes in full-depth results. Auto-set when session dev mode is active.
    required: false
tags:
- context
- knowledge
aliases:
- knowledge
- search everything
- find
- personal patterns
- blindspots
- metacognition
- system docs
- unified search
parents:
- context
- context
---
