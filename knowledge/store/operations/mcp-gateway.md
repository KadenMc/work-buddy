---
name: MCP Gateway
kind: directions
description: How to discover and invoke agent-invocable Skills and Workflows through the MCP gateway
summary: 'FastMCP gateway on port 5126: initialize the session, discover an agent-invocable Skill or Workflow, invoke it, advance or inspect Workflow runs, and retrieve elided results. Use this boundary over raw Python imports.'
trigger: agent needs to interact with work-buddy systems or discover skills
tags:
- mcp
- gateway
- wb_search
- wb_run
- wb_init
aliases:
- wb_search
- wb_run
- wb_init
- wb_advance
- wb_status
- gateway tools
- MCP tools
parents:
- operations
- operations
---

FastMCP server exposing agent-invocable work-buddy Skills and Workflows through a small gateway surface with dynamic discovery. It runs as a persistent service on `localhost:5126`, shared across agent sessions in this project.

## Before writing Python: check the gateway

Before writing Python to interact with the vault, tasks, journal, contracts, memory, or any work-buddy state — **check the gateway first**. Many operations already exist as registered skills. Do not guess at Python imports or invent APIs.

| Tool | Purpose |
|------|---------|
| `mcp__work-buddy__wb_init(session_id)` | **REQUIRED first call.** Registers your session with the gateway. Pass your `WORK_BUDDY_SESSION_ID`. |
| `mcp__work-buddy__wb_search(query)` | **Discover OR inspect.** Natural language ranks unified-store hits, including Skills, Workflows, and compatibility knowledge previews. An exact Skill name, Workflow name, Workflow alias, or stable Workflow ID short-circuits to registry metadata and returns the matching schema. |
| `mcp__work-buddy__wb_run(skill, params)` | Execute a discovered Skill or start a Workflow. `skill` is the compatibility-stable argument name; params may be a JSON string or dict. |
| `mcp__work-buddy__wb_advance(workflow_run_id, step_result)` | Complete the current reasoning step and advance the Workflow DAG. |
| `mcp__work-buddy__wb_status(workflow_run_id?, operation_id?)` | Check one Workflow run, inspect one operation record, or—when neither ID is supplied—check system health and active Workflows. |
| `mcp__work-buddy__wb_step_result(workflow_run_id, step_id, key?)` | Retrieve full step result data elided by the visibility system. |
| `mcp__work-buddy__wb_skill_result(operation_id, key?)` | Retrieve the full result of a skill call that the dispatch-size cap truncated to a marker — whole, or a single top-level key. The skill-side twin of `wb_step_result`. |
| `mcp__work-buddy__wb_capability_result(operation_id, key?)` | Deprecated compatibility alias for `wb_skill_result`; new callers use `wb_skill_result`. |

These are **MCP tools**, not Python functions. They appear in the tool list as `mcp__work-buddy__wb_run`, `mcp__work-buddy__wb_search`, etc. **Always prefer these MCP tools over Python code** for work-buddy skills and workflows.

## Session initialization (mandatory)

**Every agent session MUST call `wb_init` before any other `wb_*` tool.** All other gateway tools return an error until `wb_init` is called. This registers your agent session with the MCP server so activity is tracked per-session.

```
mcp__work-buddy__wb_init(session_id="<your WORK_BUDDY_SESSION_ID>")
```

If `wb_init` is not in your tool list (e.g., resumed session with cached tools):

```
mcp__work-buddy__wb_run(skill="wb_init", params={"session_id": "<your WORK_BUDDY_SESSION_ID>"})
```

`WORK_BUDDY_SESSION_ID` is set automatically by the SessionStart hook; read it from conversation context or the environment.

## Standard discovery flow

`wb_init` → `wb_search` to discover → read the parameter schema in the search result → `wb_run` to invoke.

**Inspect before calling an unfamiliar action.** `wb_search("task_create")` with an exact Skill name returns just that entry with its full parameter schema. Exact Workflow lookup also accepts its canonical `workflow_name`, any executable `workflow_aliases` address, or its immutable `workflow_id`. Do not guess parameter names or Workflow addresses.

**Performance caveat:** `wb_search` can hang when the embedding service is cold (5+ minutes observed). When you already know the skill name, use `wb_run` directly and skip search.

## Do not

- **Guess at `work_buddy.*` module paths or function signatures** — search first.
- **Write raw Python to read vault files** when a gateway skill already exists.
- **Write Python to call work-buddy functions** when the same operation is available as an MCP tool.
- **Skip `wb_init`** — all other tools are gated behind it.

## Hack around missing MCP tools — don't

If neither `mcp__work-buddy__wb_init` nor the documented `wb_run(skill="wb_init", ...)` compatibility fallback is available, stop immediately and tell the user. Do **not** attempt raw Python imports, async function calls from the CLI, manual JSON file reads, grepping vault files, writing to vault paths, curling sidecar ports, or any other workaround — none of them work.

Diagnose and fix via these steps:

1. Run `echo $CLAUDE_CODE_ENTRYPOINT` via Bash.
2. If it contains `desktop` → tell the user to press **Ctrl+R** to reconnect MCP servers.
3. Otherwise (CLI) → tell the user to run **`/mcp`** to reconnect.
4. If the sidecar itself is down, they'll also need to restart it first.

### `wb_run` is the invocation contract, not a convenience wrapper

If a Skill or Workflow is registered in the gateway, `wb_run` is the agent-facing way to invoke it. Calling underlying Python directly bypasses session tracking, admission, consent gates, operation logging, and retry policy. The operation is **not equivalent** even if the outcome looks the same.

## Workflow identity and admission

A Workflow definition has four distinct identifiers:

- `workflow_id` — immutable opaque definition identity (`wfd_` plus 32 lowercase hexadecimal characters).
- `workflow_name` — mutable primary invocation address.
- `workflow_aliases` — durable alternate invocation addresses, including former canonical names after a rename.
- `workflow_revision` — deterministic `sha256:` revision of the serialized authored definition snapshot, directly bound Directions snapshot, and compiled child-Workflow target identities. It is computed, not authored.

Every invocation gets a separate `workflow_run_id` (`wf_...`). A successful Workflow start returns the canonical name, stable definition ID, computed revision, and run ID; `wb_status` preserves the same identity fields for that run.

Both MCP and sidecar adapters cross `WorkflowService`. It resolves through the shared registry, performs address-first authorization checks, aggregates structured admission failures (context, mode, preference, component, and executor-facility constraints), validates params, then coordinates consent. A denial creates no run. On success, the service starts the conductor with the stable definition ID and expected revision so a concurrent definition change cannot silently start different content.

In a session with a restrictive MCP ACL, authorization deliberately evaluates the submitted address before registry resolution so known and unknown addresses have the same denial shape. Use the canonical Workflow address exposed by that ACL; an alias or stable ID is not normalized to its canonical name before this anti-oracle check unless that address is itself present in the ACL. Unrestricted MCP sessions and sidecar jobs may use canonical names, executable aliases, or stable IDs normally.

## Dispatch reliability — timeouts and the bridge circuit breaker

Every `wb_run` dispatch runs under an operation-appropriate wall-time budget and emits timing telemetry. Two failure responses you may see carry a distinct `error_kind`:

- **`mcp_gateway_timeout`** — the skill did not return within its dispatch budget (most local skills: 30s; some declare their own). The work may still be running in the background, so treat the outcome as unknown rather than failed; retry only if the operation is idempotent. The budget is a property of the operation, not something you set per call.
- **`obsidian_bridge_circuit_open`** (also `bridge_circuit_open: true`) — Obsidian-bridge skills are governed by a shared circuit breaker. After repeated bridge failures the breaker opens and sheds further bridge calls instead of hammering a struggling bridge; it admits a probe again automatically after a short cooldown. If you see this, the bridge is unhealthy (check that Obsidian is running with the bridge plugin enabled) — wait and retry rather than looping immediately.

A skill whose bridge is momentarily down fails fast per call with an actionable error and recovers the instant the bridge returns — no registry reload needed.

An oversized result is also handled gracefully rather than blowing the response: when a skill's serialized result exceeds the inline cap (`gateway.result_cap_chars`, default 100000), `wb_run` returns a `{_truncated, _size, _keys, _operation_id, _message}` marker instead of the full payload. The full result is preserved in the operation record — retrieve it whole or by a single top-level key with `wb_skill_result(operation_id[, key])`. This is the skill-dispatch analogue of the workflow visibility system's `wb_step_result`.

## Gaps are OK to surface

Not everything is in the gateway yet. If `wb_search` returns nothing relevant, no matching agent-invocable action may exist; using an explicitly documented lower-level package surface (or raising the gap to the user) can then be appropriate, but check first.

## Learning about the system

When you need to understand a subsystem, figure out how to accomplish something, or find the right skills for a task — **use `knowledge` or `agent_docs` before reading README files or guessing at code**. `knowledge` searches both system docs and personal knowledge; `agent_docs` searches system docs only.

```
// "How do I do X?" — search system docs by intent
mcp__work-buddy__wb_run("agent_docs", {"query": "find a past conversation"})

// "What's in this domain?" — browse a subtree
mcp__work-buddy__wb_run("agent_docs", {"scope": "tasks/"})

// "Give me the full directions for this" — direct lookup
mcp__work-buddy__wb_run("agent_docs", {"path": "morning/directions", "depth": "full"})

// Search personal knowledge (patterns, feedback, preferences)
mcp__work-buddy__wb_run("knowledge_personal", {"category": "work_pattern", "severity": "HIGH"})

// Search everything (system + personal)
mcp__work-buddy__wb_run("knowledge", {"query": "my documented work patterns"})
```

Start at `depth="index"` to scan broadly (cheap — just names and children), then drill into `summary` or `full` for what you actually need. You don't need to load entire subsystems to answer a focused question.
