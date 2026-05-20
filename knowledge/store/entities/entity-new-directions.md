---
name: Creating an Entity
kind: directions
description: How to author a new entity — canonical name, tags, aliases, description, and the post-creation ritual.
trigger: The user wants to register a person/place/org/concept, or an agent learns a durable fact worth recording so future agents don't re-ask; or the user runs /wb-entity-new.
command: wb-entity-new
capabilities:
- entities/entity_create
- entities/entity_resolve
tags:
- entities
- create
- directions
aliases:
- create entity directions
- new entity directions
- register entity directions
parents:
- entities
---

`entity_create` authors a new entity. Entities are **authored-only** — they exist because a person or an agent created them. The act of creation is the act of confirmation; there is no separate candidate/confirm step.

## Before creating: resolve first

Always call `entity_resolve` on the name first. If a match already exists, do NOT create a duplicate — either it is the same thing (use it) or the new name is a variant (add it as an alias via `entity_add_alias`).

## Parameters

- **canonical_name** (required) — the display name, e.g. `Max McKeen`. Stored as-is; a normalized form (lowercase, collapsed whitespace) drives case-insensitive uniqueness. Two entities cannot share a normalized name.
- **description** — free-form prose. This is where relationship context lives: "Max McKeen — Kaden's younger brother." Write the description so a cold agent reading it understands what the entity means to the user.
- **tags** — hierarchical, multi-valued. `person`, `person/family`, `person/colleague`, `place/work`, `institution`. Prefer specific tags; the hierarchy means `person/family` is still found by a `person` filter.
- **aliases** — alternative names that should resolve to this entity. Each alias is globally unique across the registry.
- **author** — `user` (default) or `agent`. Agent-authored creates are consent-gated; the user approves the pattern once per cache window.

## Slug-free by design

Entities have no slug. The integer `id` is the stable identifier; the canonical name is a renameable display label. Do not invent a slug.

## Post-creation

Confirm the entity back to the user with its id. If you created it because the user just explained an unfamiliar reference, say so plainly: the point is that the next agent will not have to ask.

## What NOT to create

Do not create an entity for something that is already a project — `entity_resolve` federates over the project registry, so projects resolve for free. Do not mirror a project into an entity row. Do not create entities speculatively from a vault scan; v1 is authored-only and a corpus scanner is explicitly out of scope.
