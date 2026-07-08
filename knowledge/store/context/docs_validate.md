---
name: Docs Validate
kind: capability
description: "Validate the knowledge store: structural integrity (DAG, command/store mappings, thinned-command format, store-path validity, required and kind-specific fields, directions fields, placeholder duplicates, parent-child symmetry, capability op-resolution, workflow step-DAG and reasoning-step consistency, directions-to-workflow and workflow-delegation binding resolution) plus the advisory durable-surfaces content audit (transient identifiers in unit prose)."
capability_name: docs_validate
category: context
op: op.wb.docs_validate
schema_version: wb-capability/v1
parameters:
  checks:
    type: str
    description: 'Comma-separated check names to run. Empty = all. Available: dag_integrity, command_mapping, thinned_commands, store_path_validity, required_fields, directions_fields, kind_specific_fields, placeholder_duplicate, durable_surfaces, parent_child_symmetry, capability_op_resolution, workflow_step_dag, workflow_step_consistency, directions_workflow_resolution, workflow_delegation_resolution. durable_surfaces emits advisory warnings only (never blocks).'
    required: false
tags:
- context
- docs
- validate
aliases:
- validate store
- check knowledge
- store health
- integrity check
- knowledge validation
parents:
- context
---
