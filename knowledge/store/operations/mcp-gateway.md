---
name: MCP Gateway
kind: directions
description: How to discover and call MCP gateway capabilities — the primary interface for agents
summary: '5-tool FastMCP gateway on port 5126: wb_init (required first call), wb_search (discover+inspect), wb_run (execute), wb_advance (workflow step), wb_status. Use these over raw Python imports.'
trigger: agent needs to interact with work-buddy systems or discover capabilities
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

FastMCP server exposing work-buddy capabilities via 5 gateway tools with dynamic tool discovery. Runs as a persistent sidecar service on `localhost:5126`, shared across all Claude Code sessions in this project.

## Before writing Python: check the gateway

Before writing Python to interact with the vault, tasks, journal, contracts, memory, or any work-buddy state — **check the gateway first**. Many operations already exist as registered capabilities. Do not guess at Python imports or invent APIs.

| Tool | Purpose |
|------|---------|
| `mcp__work-buddy__wb_init(session_id)` | **REQUIRED first call.** Registers your session with the gateway. Pass your `WORK_BUDDY_SESSION_ID`. |
| `mcp__work-buddy__wb_search(query)` | **Discover OR inspect.** Natural language → find capabilities. Exact name → get its full parameter schema. |
| `mcp__work-buddy__wb_run(capability, params)` | Execute a discovered capability. Params: JSON string or dict. |
| `mcp__work-buddy__wb_advance(workflow_run_id, result)` | Step through multi-step workflows. |
| `mcp__work-buddy__wb_status()` | Check system health and active workflows. |
| `mcp__work-buddy__wb_step_result(workflow_run_id, step_id, key?)` | Retrieve full step result data elided by the visibility system. |

These are **MCP tools**, not Python functions. They appear in the tool list as `mcp__work-buddy__wb_run`, `mcp__work-buddy__wb_search`, etc. **Always prefer these MCP tools over Python code** for work-buddy capabilities and workflows.

## Session initialization (mandatory)

**Every agent session MUST call `wb_init` before any other `wb_*` tool.** All other gateway tools return an error until `wb_init` is called. This registers your agent session with the MCP server so activity is tracked per-session.

```
mcp__work-buddy__wb_init(session_id="<your WORK_BUDDY_SESSION_ID>")
```

If `wb_init` is not in your tool list (e.g., resumed session with cached tools):

```
mcp__work-buddy__wb_run(capability="wb_init", params={"session_id": "<your WORK_BUDDY_SESSION_ID>"})
```

`WORK_BUDDY_SESSION_ID` is set automatically by the SessionStart hook; read it from conversation context or the environment.

## Standard discovery flow

`wb_init` → `wb_search` to discover → read the parameter schema in the search result → `wb_run` to execute.

**Inspect before calling unfamiliar capabilities.** `wb_search("task_create")` with an exact capability name returns just that one entry with its full parameter schema — no search overhead, no extra results. Do not guess parameter names.

**Performance caveat:** `wb_search` can hang when the embedding service is cold (5+ minutes observed). When you already know the capability name, use `wb_run` directly and skip search.

## Do not

- **Guess at `work_buddy.*` module paths or function signatures** — search first.
- **Write raw Python to read vault files** when a gateway capability already exists.
- **Write Python to call work-buddy functions** when the same operation is available as an MCP tool.
- **Skip `wb_init`** — all other tools are gated behind it.

## Hack around missing MCP tools — don't

If `mcp__work-buddy__wb_init` is not in your tool list, stop immediately and tell the user. Do **not** attempt raw Python imports, async function calls from the CLI, manual JSON file reads, grepping vault files, writing to vault paths, curling sidecar ports, or any other workaround — none of them work.

Diagnose and fix via these steps:

1. Run `echo $CLAUDE_CODE_ENTRYPOINT` via Bash.
2. If it contains `desktop` → tell the user to press **Ctrl+R** to reconnect MCP servers.
3. Otherwise (CLI) → tell the user to run **`/mcp`** to reconnect.
4. If the sidecar itself is down, they'll also need to restart it first.

### `wb_run` is the interface contract, not a convenience wrapper

If a capability is registered in the gateway, `wb_run` is the only valid way to invoke it — even when MCP is connected and working. Calling the underlying Python directly bypasses session tracking, consent gates, operation logging, and retry policy. The operation is **not equivalent** even if the outcome looks the same.

## Dispatch reliability — timeouts and the bridge circuit breaker

Every `wb_run` dispatch runs under an operation-appropriate wall-time budget and emits timing telemetry. Two failure responses you may see carry a distinct `error_kind`:

- **`mcp_gateway_timeout`** — the capability did not return within its dispatch budget (most local capabilities: 30s; some declare their own). The work may still be running in the background, so treat the outcome as unknown rather than failed; retry only if the operation is idempotent. The budget is a property of the operation, not something you set per call.
- **`obsidian_bridge_circuit_open`** (also `bridge_circuit_open: true`) — Obsidian-bridge capabilities are governed by a shared circuit breaker. After repeated bridge failures the breaker opens and sheds further bridge calls instead of hammering a struggling bridge; it admits a probe again automatically after a short cooldown. If you see this, the bridge is unhealthy (check that Obsidian is running with the bridge plugin enabled) — wait and retry rather than looping immediately.

A capability whose bridge is momentarily down fails fast per call with an actionable error and recovers the instant the bridge returns — no registry reload needed.

## Gaps are OK to surface

Not everything is in the gateway yet. If `wb_search` returns nothing relevant, then using the Python package directly (or raising a gap to the user) is acceptable — but check first.

## Learning about the system

When you need to understand a subsystem, figure out how to accomplish something, or find the right capabilities for a task — **use `knowledge` or `agent_docs` before reading README files or guessing at code**. `knowledge` searches both system docs and personal knowledge; `agent_docs` searches system docs only.

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
