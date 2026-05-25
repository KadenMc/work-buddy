---
name: Drill Tree
kind: capability
description: 'Walk a tree-shaped drillable resource at three depths (index|summary|full). Today''s domains: knowledge (units via agent_docs), summary (summarization framework''s per-node store).'
capability_name: drill_tree
category: disclosure
parameters:
  domain:
    type: str
    description: 'Registered domain name. Today: ''knowledge'' or ''summary''. Use available_domains() / inspect the disclosure system unit to see all registered.'
    required: true
  node_id:
    type: str
    description: 'Domain-specific node identifier. knowledge: unit path (e.g. ''architecture/summarization-framework''). summary: ''{namespace}:{item_id}'' for the whole tree or ''{namespace}:{item_id}#n{ordinal}'' for an internal node.'
    required: true
  depth:
    type: str
    description: '''index'' (node + child names), ''summary'' (node + each child''s summary), or ''full'' (everything). Default ''summary''.'
    required: false
op: op.wb.drill_tree
schema_version: wb-capability/v1
tags:
- disclosure
- drill
- navigation
- progressive-disclosure
- tree-walk
aliases:
- drill
- drill into
- walk tree
- navigate resource
- progressive disclosure
parents:
- disclosure
---
