---
name: Dev Doc Update Directions
kind: directions
description: How to run /wb-dev-document — scan current changes, propose knowledge-store edits, confirm, apply via docs_*/workflow_* capabilities, validate store integrity, report. Rules for what to check and what not to clobber.
trigger: When the user invokes /wb-dev-document, or as a mandatory step inside /wb-dev-pr, or after a dev change that might have made existing knowledge units stale
command: wb-dev-document
workflow: dev/dev-document
capabilities:
- agent_docs
- docs_create
- docs_update
- docs_delete
- workflow_create
- workflow_update
tags:
- dev
- document
- docs
- knowledge-store
- directions
aliases:
- doc update directions
- update docs directions
- doc hygiene rules
- when to update knowledge store
parents:
- dev
- dev
---

Update the knowledge store after dev changes. Run via `mcp__work-buddy__wb_run("dev-document")` — the workflow DAG enforces scan → propose → confirm → apply → validate → report; you don't manually sequence the doc check anymore.

## When to run

- **Before `/wb-dev-pr`** — the commit workflow invokes this as a mandatory step; you can still run it standalone first.
- **After any architectural change** that affects behavior visible in an existing knowledge unit.
- **When adding a new subsystem, capability, or workflow** — new units usually need creating.

## What gets checked

The `scan` step produces a first-pass net:
- Changed files (uncommitted + untracked).
- Classification by bucket (module / knowledge / slash / tests / config).
- `candidate_units`: knowledge units whose text mentions a changed file or subsystem slug. Ranked by match strength.

You then add the semantic layer:
- Read each candidate at `depth="full"` and judge.
- Do 2-4 `agent_docs(query=...)` semantic searches for concepts your change touches.
- Grep CLAUDE.md for keywords — it is the highest-priority surface and must not go stale.

## Choosing the right `kind` for a new unit

Before creating a new unit, decide its kind. The decision affects how it renders, how it's discovered, and what schema fields it can carry. Getting the kind right at create-time is much cheaper than reclassifying later.

Apply this decision flow in order; first match wins:

1. **Behavioral guide loaded by a slash command (`/wb-...`)** → `directions`.
2. **Callable from MCP via `wb_run`** → `capability`. Auto-generated from `registry.py`; never hand-create.
3. **Multi-step DAG the conductor advances** → `workflow`. Hand-authored in `workflows.json`; create via `workflow_create`.
4. **Personal/user-authored knowledge (Obsidian-vault-backed)** → `personal`. Create via `knowledge_mint`.
5. **Runs on a network port internal to work-buddy** → `service`. Examples: dashboard (5127), embedding service (5124), messaging (5123). Has `ports`.
6. **Primarily documents a connection to an **external** system** → `integration`. Obsidian, Thunderbird, Tailscale, Hindsight, an Obsidian plugin, etc. Even if work-buddy runs a local bridge for it, the bridge is the mechanism; the external thing is the unit's identity. Has `external_system`, `bridge_module`.
7. **Coherent functional domain whose persistent state work-buddy itself owns** (DB, files in `.data/`, etc.) → `system`. Examples: `tasks`, `triage`, `memory`. **Memory ownership is the disambiguator from `integration`** — if state lives outside work-buddy (user's vault, external server), it is an `integration`, not a `system`, even if work-buddy provides operations against it.
8. **Otherwise the unit is `reference` or `concept`.** This is the most error-prone boundary — read the next subsection carefully.

### `reference` vs `concept` — do not get this wrong

The naive rule "has `entry_points` → reference" is **wrong**. Both kinds may carry `entry_points`. Judge by the **primary purpose** of the content, not by whether code pointers exist.

A unit is **`reference`** when ALL of these hold:
- Content is short (< ~400 words is a strong signal).
- Structured around code surface: function signatures, class names, schema fields, parameter lists.
- Reader's purpose is to **look up the API**, not understand a subsystem.
- Documents 1-3 closely-related entry points.
- Examples: `automation/contexts` (one resolver function), `clarify/reference-filing` (one pipeline), `obsidian/vault-writer` (section-aware writing).

A unit is **`concept`** when:
- Content is long-form narrative (> ~400 words OR 5+ section headers is a strong signal).
- Explains **how** and **why** a subsystem works, not just what's in the code.
- Reader's purpose is to **understand a subsystem or design** — they want the mental model, not an API signature.
- Section structure spans aspects (lifecycle, recovery, surfaces, decisions) rather than entry-point listings.
- Even if `entry_points` are populated, they are pointers within the narrative, not the unit's reason for existing.
- Examples: `architecture/repo-structure` (layout narrative), `architecture/retry-queue` (subsystem behavior), `obsidian/typed-exceptions` (error-classification policy), `obsidian/vault-write-decision` (architectural decision tree).

The quick test: **"Would a reader consult this to call an API, or to understand how something works?"** API → `reference`. Understanding → `concept`.

### Multi-parent nesting for systems

A `system` unit can be a subsystem of another `system`. The `parents` field is a **list**, not a singleton; the DAG validator allows multiple parents. When creating a `system` unit that nests under a larger system, include the parent system in `parents` regardless of where the path lives. Path picks one navigation entry point; the DAG carries the relationship graph. The "subsystem-of-system" relationship is then derivable: walk a unit's `parents` and filter by `kind == "system"`.

## content_full vs dev_notes — choose the right field

Knowledge units have **two** body fields surfaced to different audiences. Picking the right one is not a stylistic call — it controls who sees what, and getting it wrong silently misleads agents in production.

- **`content_full`** — the canonical body. Read by **every** agent (operational + dev) on `agent_docs(depth="full")`. This is the contract.
- **`dev_notes`** — surfaced **only** when dev mode is on (set via `dev_mode_toggle`, auto-enabled by `/wb-dev`). This is the workshop.

The split is the structural mechanism for the operational/developmental separation. Get it wrong and either operational agents miss the contract, or their context window fills with implementation noise they cannot act on.

### What goes in `content_full`

Anything an agent needs to *use* the subsystem correctly:
- Public surfaces: HTTP routes, capability names, parameter shapes.
- Semantic contracts: what the API returns, idempotency, gating, error modes, the ranges of values that mean different things.
- User-visible behavior: what the UI shows, what state changes mean, what a button does.
- Cross-references to sibling units.

### What goes in `dev_notes`

Anything only useful when *modifying* the subsystem:
- Internal helper functions, snapshot/cache patterns, state-management invariants.
- Refactor footguns ("don't collapse `None` and `[]`", "this state must outlive the refetch", "lock order is X before Y").
- Why-decisions about non-obvious code — the kind of thing a code review would call out.
- Pre-existing bug history or "we tried X and it broke Y" lore.
- File paths, function names, and line-of-attack hints that only matter if you're about to edit the code.

### The decision test

For each fact you're considering writing: ask **"If an operational agent was calling this subsystem from a capability — would they want this in their context window?"**

- Yes → `content_full`.
- No, only useful while editing the code → `dev_notes`.

Borderline case rule: lean toward `content_full` if the rule has user-visible consequences (e.g. the trichotomy of a query param affects what the API returns); lean toward `dev_notes` if it only affects how the rule is *implemented*.

### Anti-patterns

- **Stuffing everything into `content_full`** — default failure mode. Pollutes operational context with implementation noise.
- **Hiding the contract in `dev_notes`** — operational agents can't see it, get the contract wrong, fail silently.
- **Duplicating the same fact in both** — drift risk. Pick the right home and cross-reference if needed.
- **Routing by which field you're already touching, not by what the new fact is** — see below.

### When updating an existing unit

Route **new** facts by their nature, not by which field is already in front of you. If you're adding both a public surface and an internal pattern to the same unit, that's two `fields` keys on the same proposal: one for `content_full`, one for `dev_notes`.

If you find yourself adding a public surface to a unit whose `content_full` is sparse but whose `dev_notes` is rich, do not bury the surface in `dev_notes` because that's where "all the writing already lives." Move it to `content_full`. Conversely, if you're adding a snapshot pattern to a unit whose `content_full` is the public contract, route that to `dev_notes` even if it's the unit's first dev_notes entry.

### Durable surfaces — no transient narrative

Both `content_full` and `dev_notes` are durable surfaces re-read by future agents. Whatever you write must describe the system's *current* behavior, not the journey of how it got there.

<<wb:dev/durable-surfaces>>

## Apply dispatch

Proposals route by unit kind:
- `directions` / `system` / `service` / `integration` / `reference` / `concept` → `docs_create` / `docs_update` / `docs_delete`
- `workflow` → `workflow_create` / `workflow_update` (the DAG fields don't fit the prose schema).

`docs_update` accepts a `kind` parameter for reclassifying an existing unit. Use it when an audit reveals a misclassification (e.g., reference → concept), not for routine field edits.

Capability units are auto-generated from `registry.py` by `python -m work_buddy.knowledge.build --write` — don't try to create/update them via `docs_*`. Instead, rebuild.

## Validate

After `apply` lands, the workflow auto-runs `docs_validate` over the whole store. This catches structural breakage the agent easily introduces by accident:
- DAG integrity (cycles, missing parents/children).
- Command-to-store mappings (every `wb-*.md` slash command needs a matching directions unit).
- Required fields per kind (directions units need a trigger; workflow units need steps).
- Parent-child symmetry (if A lists B as a parent, B must list A as a child).
- Dangling path references.
- **Placeholder duplicates** — the same `<<wb:X>>` target appearing more than once within a single unit. Hard error: duplicates render as back-reference markers at read time and contribute zero readable content, so they're never the right authorial choice. The editor pre-rejects them at write time; this corpus-wide check catches direct-JSON bypasses.

The `report` step surfaces any failures. Fix them **in the same run** with a follow-up `docs_update` or `workflow_update` — don't commit with a broken store.

## Rules

- **No clobbering** — `update` only changes the fields that are stale.
- **No recency bias** — a unit describes its whole feature, not just your latest edit.
- **Empty proposals are valid** — don't invent work if nothing is stale. But "I can't find anything" ≠ "I didn't look"; you must have loaded the top candidate_units and done semantic searches first.
- **Never hand-edit `knowledge/store/*.json`** — the `apply` step uses the sanctioned capabilities for a reason (DAG validation, parent-child reconciliation, cache invalidation, and the placeholder-duplicate pre-write reject).
- **Validation failures block the commit** — not prescribed by a machine gate (`validate` is auto_run, not halting), but treat them as blocking: a broken store misleads every future agent.
