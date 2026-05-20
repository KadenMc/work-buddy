---
name: Entity Create
kind: capability
description: Create a new entity with optional description, tags, and aliases. Consent-gated for agent-author writes. Optionally anchors an initial reference if source_path + source_kind are supplied.
capability_name: entity_create
category: entities
parameters:
  canonical_name:
    type: str
    description: Display name, e.g. 'Ada Lovelace'. Normalized to lowercase+collapsed-whitespace for uniqueness.
    required: true
  description:
    type: str
    description: Free-form prose. Where relationship context lives ("Ada Lovelace — the user's former manager.").
    required: false
  tags:
    type: list
    description: Hierarchical tags like 'person', 'person/family', 'place/work'. Multi-valued.
    required: false
  aliases:
    type: list
    description: Alternative names that resolve to this entity. Each alias is globally unique.
    required: false
  author:
    type: str
    description: '''user'' (default) or ''agent''. Agent-author writes are consent-gated.'
    required: false
  source_path:
    type: str
    description: Document/session/agent path. With source_kind, anchors an initial reference row.
    required: false
  source_kind:
    type: str
    description: One of document, chat, task, agent, manual.
    required: false
mutates_state: true
retry_policy: manual
consent_required: true
op: op.wb.entity_create
schema_version: wb-capability/v1
tags:
- entities
- entity
- create
aliases:
- create entity
- new entity
- add entity
- register entity
- name a thing
- record who this is
parents:
- entities
---
