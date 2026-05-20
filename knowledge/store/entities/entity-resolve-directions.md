---
name: Resolving an Entity
kind: directions
description: When and how to call entity_resolve — the pull-based lookup an agent uses on an unfamiliar proper noun before asking the user.
trigger: An agent encounters an unfamiliar proper noun (a person, place, or organization name) and needs to know what it refers to; or the user runs /wb-entity-resolve.
command: wb-entity-resolve
capabilities:
- entities/entity_resolve
tags:
- entities
- resolve
- directions
- federated
aliases:
- resolve directions
- who is
- what is
- lookup name
- entity lookup directions
parents:
- entities
---

`entity_resolve` is the **pull** half of the entity registry. The registry is deliberately NOT injected at SessionStart — instead, an agent that hits a reference it does not understand calls `entity_resolve` the same way it would call `agent_docs` or `wb_search`.

## When to call it

Call `entity_resolve` BEFORE asking the user "who/what is X?" whenever X is a proper noun naming a person, place, institution, or project that the user mentions as if you should already know it. The whole point of the registry is that the user should not have to re-explain Max, SickKids, or ElectricRAG every session.

## How to call it

```
mcp__work-buddy__wb_run("entity_resolve", {"query": "Max"})
```

The result is `{query, matches: [...], ambiguous: bool}`. Each match carries a `provider` field:
- `provider="entities"` — a row in the entity registry. Has `description`, `tags`, `aliases`.
- `provider="projects"` — a project in the project registry. The resolver federates over both stores.

## Interpreting the result

- **One match** — use it. The description answers "what is this."
- **`ambiguous=true`** (more than one match) — do NOT guess. Show the user the candidates and let them disambiguate. A name that is both an entity and a project legitimately surfaces twice.
- **Zero matches** — the registry does not know this reference yet. NOW ask the user. If they explain it, offer to record it with `entity_create` so the next agent does not have to ask again.

## Recording references as a side effect

If you are resolving a name while working inside a document or session context, pass `source_path` and `source_kind` so the resolution is logged in the append-only reference index:

```
mcp__work-buddy__wb_run("entity_resolve", {"query": "Max", "source_path": "vault://daily/2026-05-19.md", "source_kind": "document"})
```

`source_kind` is one of `document`, `chat`, `task`, `agent`, `manual`. The reference recorder de-duplicates within an hour per `(entity, source_path, source_kind)`, so resolving the same name repeatedly in one session does not spam the index.

References are recorded only for `entities`-provider matches — project matches do not create entity references.
