---
name: Docs Validate
kind: capability
description: 'Validate the knowledge store: DAG integrity, command-to-store mappings, thinned command format, required fields, kind-specific fields, placeholder duplicates, and parent-child symmetry.'
capability_name: docs_validate
category: context
op: op.wb.docs_validate
schema_version: wb-capability/v1
parameters:
  checks:
    type: str
    description: 'Comma-separated check names to run. Empty = all. Available: dag_integrity, command_mapping, thinned_commands, store_path_validity, required_fields, directions_fields, kind_specific_fields, placeholder_duplicate, parent_child_symmetry'
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
