---
name: MCP Gateway Design Tenets
kind: directions
description: Five architectural principles for designing capabilities, plus the priming hazard and agentic stub patterns for workflow authoring
summary: Five design principles for capabilities (Progressive Disclosure, JIT Retrieval, Programmatic Offloading, One Capability per Concept, Slash Command Coverage), plus the priming hazard concept and the agentic stub pattern for writing workflow step instructions.
trigger: When adding new capabilities or workflows, or when deciding how to structure MCP gateway interactions
tags:
- dev
- developmental
- mcp
- gateway
- design-tenets
- capabilities
- workflows
aliases:
- design tenets
- capability design principles
- gateway design
- priming hazard
- agentic stub pattern
- workflow authoring
parents:
- dev
- dev
dev_notes: Gateway tool functions (wb_run, wb_init, wb_search, etc.) must return dicts via _prepare(), never JSON strings via json.dumps(). The MCP SDK serializes automatically via pydantic_core.to_json(result, fallback=str, indent=2) — see func_metadata.py line 531. Using json.dumps() in a return path causes double-serialization (the string gets re-serialized by the transport). _prepare() recursively converts Path→posix, date/datetime→isoformat, sets→sorted lists, returning native Python types. See tests/unit/test_gateway_prepare.py for the regression suite.
---

## Capability Design Principles

### 1. Progressive Disclosure
Register many small capabilities rather than few large ones. Each collector is its own capability (`context_git`, `context_chat`, etc.); the full bundle (`context_bundle`) exists for agents that want everything.

### 2. Just-in-Time Retrieval
Capabilities return data directly as strings or dicts, not file paths. An agent should get the answer from `wb_run("context_chat")`, not be told "go read this file."

### 3. Programmatic Offloading
If a task is deterministic and unit-testable, it's a capability, not a workflow step. The `collect-and-orient` workflow's step 1 (run all collectors) is pure code — it became the `context_bundle` capability. Steps 2-5 require LLM reasoning and stay as workflow steps.

### 4. One Capability per Concept
Prefer a single capability with optional parameters over multiple near-identical capabilities. Only split when parameter schemas are genuinely different or operations serve distinct intents. Example: `day_planner` handles status/read/generate/write via its `action` param rather than registering 5 separate capabilities.

### 5. Slash Command Coverage
Every user-facing capability must have a corresponding slash command in `.claude/commands/`. When adding a new capability, add the slash command and update the table in `CLAUDE.md` in the same commit.

## The Priming Hazard

Python code blocks in operational workflow instructions are a **priming hazard**: they teach agents to bypass the gateway even when CLAUDE.md says not to. If a workflow step shows `from work_buddy.contracts import ...`, the agent will use that import. If it shows `wb_run("contract_health")`, it will use the gateway. **Agents follow what they read.**

The gateway-first rule is about how we **instruct** operational agents, not about developing agents. Dev agents use gateway tools AND write/execute Python. But `workflows.json` step instructions that operational agents follow at runtime must route through the gateway.

The only acceptable Python in workflow step instructions is:
- **Pure formatting** (e.g., `format_briefing()`) — transforms data already in memory, could become auto_run
- **Operations with no gateway capability yet** — annotated with a note explaining why

Everything else goes through `wb_run()`.

## The Agentic Stub Pattern

For `step_type: reasoning` steps in workflow definitions, keep instructions minimal — behavioral guidance lives in the knowledge store `directions` unit that the slash command loads. The step instruction describes **what** to do; the directions unit describes **how**.

This works because slash commands load directions into the agent's context as first-class instructions via `agent_docs`. Workflow step instructions arrive as MCP tool results, which carry less instructional weight. Put quality bars, synthesis rules, tone guidance, and don’ts in the directions unit, not in step instructions.

### What goes in workflow step instructions (workflows.json)
- MCP call signatures and sequences
- Data contracts between steps (structured dicts, not free text)
- DAG structure (steps, deps, auto_run)

### What goes in knowledge store directions units
- Synthesis rules, quality bars, tone
- Approval gates, don’ts, anti-patterns
- Behavioral guidance the agent must internalize
