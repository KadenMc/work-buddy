---
name: Knowledge Personal
kind: capability
description: Search personal knowledge from the Obsidian vault. Includes minted insights, patterns, feedback, preferences. Supports filtering by category and severity.
capability_name: knowledge_personal
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
    description: Path prefix (e.g. 'personal/metacognition/').
    required: false
  category:
    type: str
    description: 'Filter by category: work_pattern, self_regulation, skill_gap, feedback, preference, reference.'
    required: false
  severity:
    type: str
    description: 'Filter by severity: HIGH, MODERATE, LOW.'
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
    description: Include dev_notes. Auto-set in dev mode.
    required: false
tags:
- context
- knowledge
- personal
aliases:
- personal knowledge
- my patterns
- calibration
- metacognition patterns
- blindspot patterns
- feedback
- preferences
- vault knowledge
parents:
- context
- context
---
