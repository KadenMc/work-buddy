---
name: Entities
kind: system
description: Entity registry — a reference-resolution layer for the user's named world. Authored, tagged, federated entity_resolve, append-only reference index.
entry_points:
- work_buddy.entities.store
- work_buddy.entities.migrations
- work_buddy.mcp_server.context_wrappers
- work_buddy.mcp_server.ops.entities_ops
tags:
- entities
- registry
- resolution
- memory
- references
---

The entity registry holds canonical names, descriptions, hierarchical tags, and aliases for the named things in the user's world (people, places, institutions, projects, concepts) so an agent never has to be re-told basic facts. It is NOT a memory or transcript system; it stores reference identity only — "what does this name mean to the user."

Four features ship in v1:

1. **Authored-only entities.** Entities exist because an agent or the user created them. No corpus scanner, no LLM extraction. The act of creation is the act of confirmation. Descriptions are free-form prose and are where relationship context lives ("Max McKeen — Kaden's younger brother.").

2. **Hierarchical multi-valued tags.** `person`, `person/family`, `person/colleague`, `place/work`. Multiple tags per entity. The `entity_list` filter is hierarchical: `tag=person` matches every entity tagged at `person` OR `person/*`. Tags are the v1 substitute for an enum-typed `type` column; they characterize the entity more richly and survive future kinds without a schema migration.

3. **Federated `entity_resolve`.** A single capability looks up a name across multiple resolution sources in parallel and merges results. v1 wires two providers: the entity store and the project registry — which already implements the entity-result shape (canonical slug + aliases + description) for free. The federation is parallel, not fallback: if a name is both an entity and a project, both matches surface, flagged by provider, and the agent disambiguates. New providers (contracts, users, …) plug in by appending to `_RESOLUTION_PROVIDERS` in `context_wrappers.py`.

4. **Append-only reference index.** Every time an agent resolves or authors an entity in a document context (passing `source_path` + `source_kind`), a row is appended to `entity_references` recording where and when the mention occurred. Nothing deletes a reference; document evolution does not retroactively erase the historical mention. A de-dup window (default 3600s per `(entity, source_path, source_kind)`) prevents intra-session spam.

**Storage.** SQLite at `<data_root>/db/entities.db`. Four tables: `entities`, `entity_tags`, `entity_aliases`, `entity_references`. Migrations live in `work_buddy/entities/migrations.py`; CRUD in `work_buddy/entities/store.py`.

**MCP surface.** Eleven capabilities: `entity_resolve` (federated), `entity_create`, `entity_update`, `entity_delete` (consent-gated), `entity_get`, `entity_list`, `entity_set_tags`, `entity_add_alias`, `entity_remove_alias`, `entity_add_reference`, `entity_list_references`. Wrappers in `work_buddy/mcp_server/context_wrappers.py`; op bindings in `work_buddy/mcp_server/ops/entities_ops.py`.

**Pull, not push.** Per the gateway's just-in-time-retrieval tenet, entities are NOT injected at SessionStart. Agents call `entity_resolve` when they hit an unfamiliar reference. The CLAUDE.md directive instructs every agent to try `entity_resolve` before asking the user about an unfamiliar proper noun.

**Dashboard.** A new top-level "Memory" tab carries an Entities sub-view modeled on the Projects view: browse, create, edit, edit tags, edit aliases, delete. The Memory tab structure is deliberately positioned to absorb future memory-shaped sub-views (Contracts, Projects) if the user later decides to consolidate; this is documented as a future option but is explicitly NOT v1 work.

**Out of scope for v1.** Corpus scanning, LLM extraction, candidate-discovery flow, SessionStart injection, structured entity-to-entity relations, migrating Projects/Contracts under Memory, migrating project aliasing. All are deliberate v2-or-later items.

File layout: `work_buddy/entities/{__init__.py, store.py, migrations.py}`; wrappers + ops; dashboard tab module; `entities/` knowledge-store scope.
