---
name: Listing Entities
kind: directions
description: How to browse the entity registry — hierarchical tag filter, presentation, drill-down via entity_get.
trigger: The user wants to see the entities they've registered, or browse by tag; or the user runs /wb-entity-list.
command: wb-entity-list
capabilities:
- entities/entity_list
- entities/entity_get
tags:
- entities
- list
- directions
- browse
aliases:
- list entities directions
- browse entities directions
- who do I know
parents:
- entities
---

`entity_list` returns entities ordered by most-recently-updated (or most-recently-referenced — recording a reference touches `updated_at`).

## Tag filter is hierarchical

`entity_list(tag="person")` returns everything tagged `person` AND everything tagged `person/family`, `person/colleague`, etc. To narrow, pass the deeper tag: `entity_list(tag="person/family")`.

## Presentation

Group the results by their top-level tag (the first slash-segment) when presenting to the user — `person`, `place`, `institution`. Show canonical name, a one-line description preview, and alias count per entity. For the full record of one entity (tags, aliases, recent references), call `entity_get`.

## entity_get enrichment

`entity_get` accepts a canonical name, an alias, or an integer id. It returns the full entity plus the 5 most-recent reference rows and a total reference count — use it for the "tell me everything about X" request.
