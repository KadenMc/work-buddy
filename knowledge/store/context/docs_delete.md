---
name: Docs Delete
kind: capability
description: Delete a unit from the knowledge store. Cleans up parent/child references.
capability_name: docs_delete
category: context
op: op.wb.docs_delete
schema_version: wb-capability/v1
parameters:
  path:
    type: str
    description: Path of unit to delete
    required: true
mutates_state: true
retry_policy: manual
tags:
- context
- docs
- delete
aliases:
- delete unit
- remove knowledge
- drop knowledge entry
- delete documentation
- erase knowledge unit
- remove docs unit
parents:
- context
---
