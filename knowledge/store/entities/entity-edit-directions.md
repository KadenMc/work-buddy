---
name: Editing an Entity
kind: directions
description: How to update an entity's name/description, manage tags and aliases, and delete — plus the consent posture on destructive edits.
trigger: The user wants to rename, re-describe, retag, re-alias, or delete an entity; or the user runs /wb-entity-edit.
command: wb-entity-edit
capabilities:
- entities/entity_update
- entities/entity_set_tags
- entities/entity_add_alias
- entities/entity_remove_alias
- entities/entity_delete
tags:
- entities
- edit
- update
- delete
- directions
aliases:
- edit entity directions
- update entity directions
- delete entity directions
- retag entity
parents:
- entities
---

Editing an entity is split across five capabilities so a focused change cannot accidentally clobber an unrelated field.

## Identity — entity_update

`entity_update(entity_id, canonical_name?, description?)` changes the name and/or description. It takes the integer id, NOT the name. A rename re-normalizes and is rejected if it collides with another entity. Passing `description=""` (empty string) clears the description; omitting `description` leaves it untouched. `entity_update` deliberately does NOT touch tags or aliases.

## Tags — entity_set_tags

`entity_set_tags(entity_id, tags)` REPLACES the full tag set. To add one tag, fetch the current set with `entity_get`, append, and pass the whole list. To clear all tags, pass `[]`.

## Aliases — entity_add_alias / entity_remove_alias

Aliases are managed one at a time. `entity_add_alias` rejects an alias that collides with another entity's canonical name or another entity's alias — an alias belongs to exactly one entity. `entity_remove_alias` is a no-op if the alias is not attached.

## Deletion — entity_delete

`entity_delete` is a HARD delete: the entity and its tags, aliases, and references are removed by cascade. It is consent-gated for both user and agent authors — the prompt surfaces the entity name and how many reference rows the cascade will take. There is no soft-delete and no undo. Before calling it, confirm the id with the user and make sure they understand the reference history goes with it. If they want to keep the entity but mark it inactive, add a tag like `status/archived` instead and filter on it at read time.
