---
name: Agent Docs Rebuild
kind: capability
description: Reload the knowledge store from disk. Use after editing store JSON files or after registry changes.
capability_name: agent_docs_rebuild
category: context
op: op.wb.agent_docs_rebuild
schema_version: wb-capability/v1
parameters:
  force:
    type: bool
    description: Force full reload (default false)
    required: false
tags:
- context
- agent
- docs
- rebuild
aliases:
- reload docs
- rebuild knowledge
- refresh store
- reload knowledge store
- pick up knowledge edits
- refresh agent docs
- reindex documentation
parents:
- context
---
