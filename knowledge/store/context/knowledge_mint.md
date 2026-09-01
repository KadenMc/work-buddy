---
name: Knowledge Mint
kind: capability
description: Create or update a versioned personal-knowledge record in SQLite, with stable aliases, provenance, and idempotent mutation receipts.
capability_name: knowledge_mint
category: context
op: op.wb.knowledge_mint
schema_version: wb-capability/v1
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
    description: Full text body. If empty, builds a body from the structured fields.
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
  idempotency_key:
    type: str
    description: Stable retry key for this logical mutation.
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
---

After the personal-knowledge authority seal, this capability never creates or
edits a Markdown file. It writes one immutable revision plus the current SQLite
projection. Legacy logical paths remain aliases, so callers can keep using
familiar names without treating a filesystem path as identity.
