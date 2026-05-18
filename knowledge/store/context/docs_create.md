---
name: Docs Create
kind: capability
description: Create a new unit in the knowledge store. Writes to the appropriate JSON file, updates parent children lists, and validates DAG integrity.
capability_name: docs_create
category: context
parameters:
  path:
    type: str
    description: Unique path ID (e.g. 'tasks/my-directions')
    required: true
  kind:
    type: str
    description: 'Unit type: directions, capability, workflow, system, service, integration, reference, concept'
    required: true
  name:
    type: str
    description: Human-readable name
    required: true
  description:
    type: str
    description: One-line summary
    required: true
  content_full:
    type: str
    description: Full content text (newlines preserved)
    required: false
  content_summary:
    type: str
    description: Short summary
    required: false
  trigger:
    type: str
    description: (directions) When to use this unit
    required: false
  command:
    type: str
    description: (directions/workflow) Slash command name
    required: false
  workflow:
    type: str
    description: (directions) Linked workflow path
    required: false
  capabilities:
    type: str
    description: (directions) Comma-separated MCP capability paths
    required: false
  parents:
    type: str
    description: Comma-separated parent paths
    required: false
  children:
    type: str
    description: Comma-separated child paths
    required: false
  tags:
    type: str
    description: Comma-separated search tags
    required: false
  aliases:
    type: str
    description: Comma-separated search aliases
    required: false
  dev_notes:
    type: str
    description: Development-facing notes surfaced only in dev mode (set via dev_mode_toggle). Use for architectural constraints, non-obvious dependencies, and hard-won lessons future dev agents could clobber.
    required: false
  entry_points:
    type: str
    description: (system kind) Comma-separated dotted module paths that implement this system, for navigation.
    required: false
  requires:
    type: str
    description: Comma-separated tool/component IDs the unit needs (e.g. 'obsidian,hindsight').
    required: false
  op:
    type: str
    description: (capability kind) op.<namespace>.<name> ID of the Op this declaration-based capability wraps.
    required: false
  schema_version:
    type: str
    description: (capability kind) Declaration format version, e.g. 'wb-capability/v1'.
    required: false
  capability_name:
    type: str
    description: (capability kind) MCP dispatch name, e.g. 'task_read'.
    required: false
  category:
    type: str
    description: (capability kind) Registry category, e.g. 'tasks'.
    required: false
  parameters:
    type: str
    description: '(capability kind) Parameter schema as a JSON string: {name: {type, description, required}}.'
    required: false
  mutates_state:
    type: bool
    description: (capability kind) Whether the capability modifies state.
    required: false
  retry_policy:
    type: str
    description: (capability kind) 'manual' | 'replay' | 'verify_first'.
    required: false
  consent_required:
    type: bool
    description: (capability kind) Whether the capability is consent-gated.
    required: false
mutates_state: true
retry_policy: manual
tags:
- context
- docs
- create
aliases:
- create unit
- add knowledge
- new docs entry
- write new knowledge
- author docs entry
- add documentation unit
- new knowledge unit
parents:
- context
- context
---
