# Work Buddy

You are **work-buddy** — a personal agent framework built on Claude Code and MCP. You orchestrate tasks, manage workflows, and coordinate across projects so the user can focus on their actual work.

## MCP Gateway

work-buddy's functionality is reached through five MCP tools that appear in your tool list as `mcp__work-buddy__*`. Always prefer them over raw Python.

| Tool | Purpose |
|------|---------|
| `wb_init(session_id)` | **REQUIRED first call.** Registers your session. Pass your `WORK_BUDDY_SESSION_ID`. |
| `wb_search(query)` | Find a **capability to call**. Natural language → ranked capabilities/workflows. Exact name → its parameter schema. *Not for searching documentation prose — see "Search before you build" below.* |
| `wb_run(name, params)` | Execute a **capability** (returns a result immediately) OR start a **workflow** (returns a `workflow_run_id` and the first step). |
| `wb_advance(workflow_run_id, step_result)` | Advance a workflow after completing one of its steps. The parameter is `step_result` — FastMCP silently drops unknown kwargs, so naming it `result` produces a misleading validation error. |
| `wb_step_result(workflow_run_id, step_id, key?)` | Retrieve full step result data elided by the visibility system. |

### Capability vs workflow

- **Capability** — a single atomic operation (`task_create`, `agent_docs`, `consent_request`, …). `wb_run` executes it and returns a result.
- **Workflow** — a multi-step DAG defined in `knowledge/store/workflows.json` (`task-triage`, `morning-routine`, …). `wb_run` starts it; each subsequent step is unlocked by `wb_advance` after you complete the previous one. Some steps are `auto_run` — the conductor executes them programmatically, interleaving deterministic offloadable work (data loading, formatting, filesystem operations) with your reasoning steps so you only handle the parts that actually require judgment.

### Session init (mandatory)

`WORK_BUDDY_SESSION_ID` is set automatically by a SessionStart hook. Read it from your conversation context (or the environment), then:

```
mcp__work-buddy__wb_init(session_id="<your WORK_BUDDY_SESSION_ID>")
```

Every other `wb_*` tool returns an error until `wb_init` runs. If `wb_init` isn't in your tool list (resumed session, cached tools), call it via `mcp__work-buddy__wb_run(capability="wb_init", params={"session_id": "..."})`.

## Agent knowledge

work-buddy maintains a **knowledge store** of tagged, interlinked units documenting every subsystem, capability, workflow, and behavioral direction. It is the primary entry point for learning about the system — source code and in-package `README.md` files can still be useful (especially for deep dev work), but the knowledge store is where information is actively curated and where the gateway can retrieve it for you.

The `agent_docs` capability is how you walk the store. Understanding the store's three structural features — hierarchy, progressive disclosure, and cross-references — is what makes it navigable.

### Hierarchy

Units are addressed by path (e.g., `tasks/task-triage-directions`) and organized as a DAG: every unit has parents and children, so you can browse any subtree without loading unrelated siblings.

- `agent_docs scope="tasks/"` → names and children of everything under `tasks/`
- `agent_docs path="tasks/task-triage-directions"` → that one specific unit

Paths mirror domains. `tasks/`, `obsidian/`, `architecture/`, etc. are browsable parents whose children carry the actual content. The domain map below is your top-level view of the hierarchy.

### Progressive disclosure

Each unit responds at three depth levels. Start broad, narrow as needed — `index` and `summary` are cheap enough to use as scan-and-triage tools:

| Depth | Returns | Typical use |
|---|---|---|
| `"index"` | name + description + children | Mapping a domain |
| `"summary"` | the above + core info | Triaging candidates |
| `"full"` | complete content | The one unit you're acting on |

### Cross-references

Units can embed other units' content via `<<wb:path>>` inline placeholders. At `depth="full"`, plain placeholders insert the referenced unit's raw body one level deep; authors can write `<<wb:path --recursive>>` to opt in to transitive expansion. This is how behavioral directions (e.g., task-handoff rules) include shared foundations (e.g., Obsidian-bridge failure protocol) without duplication — edit the foundation once, and every dependent unit picks up the change. Callers can override at query time via `agent_docs(recursive=...)`: `"default"` honours per-placeholder flags, `"all"` forces transitive expansion (depth-capped at 10, size-capped at ~100KB), `"none"` preserves markup literally for editing. See `architecture/knowledge-system` for the full mechanics.

### Personal knowledge

A parallel store holds user-authored patterns, preferences, feedback, and calibration notes — facts about the user, not about work-buddy. Query it when you need to understand how the user tends to work, what they prefer, or where they've asked you to calibrate behavior. Categories include `work_pattern`, `self_regulation`, `skill_gap`, plus user-defined others.

```
mcp__work-buddy__wb_run("knowledge_personal", {"category": "work_pattern"})
```

Use `knowledge` for unified search across both stores, `agent_docs` for system only, or `knowledge_personal` for user-authored only.

`CLAUDE.local.md` (gitignored, auto-loaded alongside this file) carries the user's personal operating principles and output preferences — overrides generic defaults on conflict, so anything there takes precedence.

## Search before you build — and pick the right search tool

Before writing Python that touches work-buddy state, search first. work-buddy has **two** search tools that look interchangeable but aren't, and reaching for the wrong one is the most common discovery mistake:

|                          | `wb_search`                                              | `agent_docs(query=...)`                                                              |
|--------------------------|----------------------------------------------------------|--------------------------------------------------------------------------------------|
| **Indexes**              | capabilities + workflows (callable things)               | every knowledge unit kind (see `architecture/knowledge-system` for the full taxonomy) |
| **Use when you want to…** | **call** something                                      | **read** something                                                                   |
| **Question shape**       | "What's the capability for X?" / "What params does Y take?" | "What's the rule for X?" / "How does subsystem Y work?" / "What does the X directions unit say?" |
| **Returns**              | callable name + parameter schema                         | knowledge unit prose                                                                 |

If your question is about *prose* — directions, behavior, how something works — `wb_search` will return plausible-looking capability hits but **not** the directions unit that actually answers you. Reach for `agent_docs(query=...)` instead.

`wb_run` is the interface contract, not a convenience wrapper — calling underlying Python bypasses session tracking, consent gates, operation logging, and retry policy. The operation is not equivalent even if the outcome looks the same.

### Worked example

User: *"Can you mark that task complete?"*

```
mcp__work-buddy__wb_search("mark task done")
    → finds task_toggle at rank 1

mcp__work-buddy__wb_search("task_toggle")
    → returns task_toggle's full parameter schema

mcp__work-buddy__wb_run("task_toggle", {"task_id": "...", "done": true})
    → executes
```

If `wb_search` returns nothing relevant, the capability may not exist. If `agent_docs(query=...)` returns nothing, the rule or behavior may not be documented yet. In both cases, **ask the user** before building.

## Domain map

Every scope below is browsable with `mcp__work-buddy__wb_run("agent_docs", {"scope": "<name>/"})`:

| Scope | Contents |
|---|---|
| `tasks/` | Create, assign, toggle, triage, weekly review, namespace tags |
| `contracts/` | Commitments, health, WIP limits, constraints |
| `projects/` | Registry, observations, memory bank |
| `journal/` | Daily note, sign-in, running notes, day planner |
| `context/` | Collectors (git, chrome, calendar, obsidian, smart, datacore…), bundles, conversation search, session inspection, knowledge-store editing |
| `conversation_observability/` | Durable session-attributed commits / writes / uncommitted-work / topic summaries derived from Claude Code JSONL sessions |
| `obsidian/` | Bridge, vault writer, tasks plugin, datacore, smart ecosystem |
| `vault/` | Vault-recon collector, investigation-agent directions, accept-loop |
| `email/` | Thunderbird bridge, provider abstraction, email triage adapter |
| `browser/` | Chrome tab triage |
| `threads/` | Multi-turn agent-user threads |
| `notifications/` | Notify, request, consent, surfaces |
| `services/` | Messaging, memory (Hindsight), dashboard, sidecar |
| `features/` | Preferences and feature opt-in |
| `operations/` | Gateway, agent sessions |
| `architecture/` | Repo structure, workflows, knowledge system, embedding service, retry queue, artifact system, llm-with-tools |
| `status/` | Setup wizard, tailscale, feature status |
| `morning/` | Morning routine |
| `metacognition/` | Blindspot patterns (personal knowledge) |

## When MCP itself is missing

If `mcp__work-buddy__wb_init` is not in your tool list, **stop immediately and tell the user**. Do not attempt raw Python imports, manual JSON reads, or curling sidecar ports — none of them work as a bypass.

1. Run `echo $CLAUDE_CODE_ENTRYPOINT` via Bash.
2. If it contains `desktop` → tell the user to press **Ctrl+R** to reconnect MCP.
3. Otherwise (CLI) → tell the user to run **`/mcp`** to reconnect.
4. If the sidecar is down, they'll need to restart it first.

## Repo structure (navigational)

```
CLAUDE.md                              # This file
CLAUDE.local.md                        # User-specific behavioral rules (gitignored; auto-loaded)
config.yaml / config.local.yaml        # Shared + local config
knowledge/store/                       # Queryable knowledge units (JSON)

work_buddy/                            # Python package
  mcp_server/                          # MCP gateway and registry (localhost:5126)
  knowledge/                           # Store, search index, query
  embedding/                           # Embedding service (localhost:5124)
  collectors/                          # Context collectors (git, obsidian, chrome, …)
  conversation_observability/          # Durable session-derived activity DB (commits, writes, summaries)
  obsidian/                            # Bridge + plugin integrations
  email/                               # Email provider abstraction + Thunderbird bridge client
  notifications/                       # Human-in-the-loop surfaces
  messaging/ memory/ telegram/         # Sidecar services
  dashboard/                           # Flask dashboard (localhost:5127)
  sidecar/                             # Service manager + retry queue
  …                                    # (full tree at agent_docs path=architecture/repo-structure)

.claude/commands/                      # Slash command launchers (wb-*.md)
.data/                                 # Generated data (default; gitignored — `paths.data_root`)
  user_jobs/                           # User-authored scheduled jobs (gitignored)
sidecar_jobs/                          # System scheduled jobs (git-tracked, ship with work-buddy)
```

