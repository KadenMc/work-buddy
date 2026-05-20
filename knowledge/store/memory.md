---
name: Memory
kind: system
description: Personal memory subsystem — semantic store, mental models, retention, reflection
summary: 'Personal memory subsystem backed by Hindsight. Capabilities: read, write, reflect, prune. Builds a digital twin from preferences, habits, and patterns.'
tags:
- memory
- hindsight
- semantic-search
- mental-models
- personal-context
aliases:
- memory layer
- personal memory
- memory subsystem
- memory_read
- memory_write
- memory_reflect
- memory_prune
---

Persistent personal memory layer for building a digital twin. Captures soft personal context (preferences, habits, emotional state, recurring patterns) that artifacts systematically miss.

Backed by Hindsight, the external server that handles storage, semantic search, and LLM-powered reflection. See `memory/hindsight` for server components, env vars, and startup details.

## Integration paths

Two paths share one local Hindsight server:

1. **Claude Code plugin (hooks)** — ambient auto-recall before every prompt, auto-retain after responses.
2. **Python adapter** (`work_buddy/memory/`) — programmatic retain/recall/reflect for context collection, workflows, and MCP gateway capabilities.

## MCP capabilities

- `memory_read` — semantic + keyword search over stored memories
- `memory_write` — store a fact, preference, or constraint
- `memory_reflect` — LLM-powered reasoning over memories (consent-gated)
- `memory_prune` — delete memories (consent-gated, irreversible)

## Bank and tags

One personal bank (user by default, configurable via `hindsight.bank_id` in `config.yaml`). Tag taxonomy: `user:default`, `source:*`, `kind:*`, `domain:*`, `workflow:*`, `session:*`. Observations (automatic pattern consolidation) and mental models (pre-computed reflect summaries) build up the digital twin over time.

## Bank bootstrap

Call `ensure_bank()` from `work_buddy.memory.setup` to create the bank with missions, directives, and mental models. Idempotent — safe to call repeatedly.
