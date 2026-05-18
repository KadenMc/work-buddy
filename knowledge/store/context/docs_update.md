---
name: Docs Update
kind: capability
description: Update fields on an existing knowledge unit. Only provided fields are changed; omitted fields preserved.
capability_name: docs_update
category: context
parameters:
  path:
    type: str
    description: Path of unit to update
    required: true
  name:
    type: str
    description: New name
    required: false
  description:
    type: str
    description: New description
    required: false
  content_full:
    type: str
    description: New full content
    required: false
  content_summary:
    type: str
    description: New summary
    required: false
  trigger:
    type: str
    description: (directions) New trigger
    required: false
  command:
    type: str
    description: New slash command name
    required: false
  parents:
    type: str
    description: New comma-separated parents (replaces)
    required: false
  children:
    type: str
    description: New comma-separated children (replaces)
    required: false
  tags:
    type: str
    description: New comma-separated tags (replaces)
    required: false
  aliases:
    type: str
    description: New comma-separated aliases (replaces)
    required: false
  dev_notes:
    type: str
    description: New development-facing notes (surfaced only in dev mode). Pass an empty string to clear.
    required: false
  entry_points:
    type: str
    description: New comma-separated dotted module paths (replaces existing).
    required: false
  kind:
    type: str
    description: 'Reclassify the unit''s kind. Must be one of: directions, system, service, integration, reference, concept, capability, workflow, personal.'
    required: false
mutates_state: true
retry_policy: manual
tags:
- context
- docs
- update
aliases:
- update unit
- edit knowledge
- modify docs
- change knowledge unit
- patch docs
- edit documentation field
- update docs entry
parents:
- context
- context
---
