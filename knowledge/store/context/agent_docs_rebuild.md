---
name: Agent Docs Rebuild
kind: skill
description: Reload the knowledge store from disk and reindex. Use after a direct edit to a unit's .md file, or after registry changes, so the store cache and search index reflect them.
category: context
op: op.wb.agent_docs_rebuild
schema_version: wb-skill/v1
parameters:
  force:
    type: bool
    description: Force full reload (default false)
    required: false
skill_name: agent_docs_rebuild
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
