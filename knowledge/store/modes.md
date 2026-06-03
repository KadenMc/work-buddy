---
name: Modes
kind: system
description: Session modes and the mode_toggle capability
summary: Container for session modes — named toggles that gate capability and workflow availability via available_when.
tags:
- modes
- control
---

## What a mode is

A **mode** is a named, per-session toggle (e.g. `dev`, `knowledge`) that gates which capabilities and workflows are discoverable and callable. Modes are declared as inert YAML under `work_buddy/modes/declarations/`; each is a `ModeDef` with an `id`, `label`, `description`, and an optional `activatable_when` constraint. A session's active modes live on its manifest.

## Gating a capability or workflow on a mode

Add an `available_when` gate-DSL string to the declaration's frontmatter:

- `available_when: knowledge` — discoverable and callable only in knowledge mode.
- `available_when: dev & knowledge` — requires both.
- `available_when: "!exploration"` — hidden while exploration mode is active.

`wb_search` omits and `wb_run` rejects (`denied_by: "mode_gate"`) a gated surface whose modes are not active; the rejection lists the `required_modes` so an agent can recover by calling `mode_toggle`. Mode ids must be gate identifiers (`[A-Za-z0-9_]+`). A capability whose `available_when` references an unknown mode fails to load loudly; a workflow with a bad gate is logged and left ungated. A declaration with no `available_when` is always available.

## Toggling

`mode_toggle(mode_id, active=None)` flips (`None`) or explicitly sets a mode and returns the full active-mode set. Activation is refused when the mode's `activatable_when` — a gate over the *other* active modes — is not satisfied (`denied_by: "activation_constraint"`).

## Reuse

Gate parsing, evaluation, and validation are `work_buddy/control/gates.py` — the same typed AST that gates dashboard cards. Mode-aware availability is the second consumer of that machinery; it does not reimplement boolean logic.
