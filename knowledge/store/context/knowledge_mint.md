---
name: Knowledge Mint
kind: capability
description: Create or update a personal knowledge unit in the Obsidian vault. Generates a markdown file with YAML frontmatter. If the file already exists, appends new evidence.
capability_name: knowledge_mint
category: context
parameters:
  name:
    type: str
    description: Human-readable name (e.g., 'Branch Explosion').
    required: true
  category:
    type: str
    description: 'Category: work_pattern, self_regulation, skill_gap, feedback, preference, reference.'
    required: true
  content_body:
    type: str
    description: Full markdown body. If empty, builds from structured fields.
    required: false
  severity:
    type: str
    description: HIGH, MODERATE, or LOW (optional).
    required: false
  tags:
    type: str
    description: Comma-separated tags.
    required: false
  evidence:
    type: str
    description: Initial evidence observation.
    required: false
  definition:
    type: str
    description: Pattern definition text.
    required: false
  triggers:
    type: str
    description: What typically triggers this pattern.
    required: false
  signals:
    type: str
    description: Observable signals.
    required: false
  default_response:
    type: str
    description: Agent's default response.
    required: false
mutates_state: true
retry_policy: manual
tags:
- context
- knowledge
- mint
aliases:
- mint
- create personal
- add pattern
- create insight
- new personal unit
- mint knowledge
- add observation
parents:
- context
- context
---
