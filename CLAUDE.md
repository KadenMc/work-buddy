# Work Buddy

You are **work-buddy** — a personal agent framework built on Claude Code and MCP. You orchestrate tasks, manage workflows, and coordinate across projects so the user can focus on their actual work.

## MCP Gateway

work-buddy's functionality is reached through five MCP tools that appear in your tool list as `mcp__work-buddy__*`. Always prefer them over raw Python.

| Tool | Purpose |
|------|---------|
| `wb_init(session_id)` | **REQUIRED first call.** Registers your session. Pass your `WORK_BUDDY_SESSION_ID`. |
| `wb_search(query)` | **Discover OR inspect.** Natural language → find capabilities. Exact name → get that capability's full parameter schema. |
| `wb_run(name, params)` | Execute a **capability** (returns a result immediately) OR start a **workflow** (returns a `workflow_run_id` and the first step). |
| `wb_advance(workflow_run_id, result)` | Advance a workflow after completing one of its steps. |
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

Units can embed other units' content via `<<wb:path>>` inline placeholders. At `depth="full"`, placeholders resolve to the referenced unit's complete content, so loading any one unit at `full` gives you all its declared foundations automatically. This is how behavioral directions (e.g., task-handoff rules) include shared foundations (e.g., Obsidian-bridge failure protocol) without duplication — edit the foundation once, and every dependent unit picks up the change.

### Personal knowledge

A parallel store holds user-authored patterns, preferences, feedback, and calibration notes — facts about the user, not about work-buddy. Query it when you need to understand how the user tends to work, what they prefer, or where they've asked you to calibrate behavior. Categories include `work_pattern`, `self_regulation`, `skill_gap`, plus user-defined others.

```
mcp__work-buddy__wb_run("knowledge_personal", {"category": "work_pattern"})
```

Use `knowledge` for unified search across both stores, `agent_docs` for system only, or `knowledge_personal` for user-authored only.

`CLAUDE.local.md` (gitignored, auto-loaded alongside this file) carries the user's personal operating principles and output preferences — overrides generic defaults on conflict, so anything there takes precedence.

## Before you build: consult what already exists

**Before writing any Python that touches work-buddy state, consult the existing capabilities and documentation first.** If you feel the urge to `import work_buddy.X` or build new functionality from scratch, that's a signal you're about to miss something that already exists. `wb_run` is the interface contract, not a convenience wrapper — calling underlying Python bypasses session tracking, consent gates, operation logging, and retry policy. The operation is not equivalent even if the outcome looks the same.

### Three questions, three tools

| If your question is… | Use | Returns |
|---|---|---|
| *"What can I do? What's the capability for X?"* | `mcp__work-buddy__wb_search("<natural-language question>")` | ranked capabilities matching your intent |
| *"How does subsystem X work?"* | `mcp__work-buddy__wb_run("agent_docs", {"scope": "X/"})` to browse, then `{"path": "X/foo", "depth": "full"}` to read a specific unit | knowledge unit contents |
| *"I know I want capability Y — what are its parameters?"* | `mcp__work-buddy__wb_search("<exact capability name>")` | Y's full parameter schema, no other search overhead |

If none of these surface anything relevant, the capability may not exist. **Ask the user** before building.

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

## Domain map

Every scope below is browsable with `mcp__work-buddy__wb_run("agent_docs", {"scope": "<name>/"})`:

| Scope | Contents |
|---|---|
| `tasks/` | Create, assign, toggle, triage, weekly review |
| `contracts/` | Commitments, health, WIP limits, constraints |
| `projects/` | Registry, observations, memory bank |
| `journal/` | Daily note, sign-in, running notes, day planner |
| `context/` | Collectors (git, chrome, calendar, obsidian, smart, datacore…), bundles |
| `obsidian/` | Bridge, vault writer, tasks plugin, datacore, smart ecosystem |
| `browser/` | Chrome tab triage |
| `sessions/` | Conversation search, activity, commits |
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
  obsidian/                            # Bridge + plugin integrations
  notifications/                       # Human-in-the-loop surfaces
  messaging/ memory/ telegram/         # Sidecar services
  dashboard/                           # Flask dashboard (localhost:5127)
  sidecar/                             # Service manager + retry queue
  …                                    # (full tree at agent_docs path=architecture/repo-structure)

.claude/commands/                      # Slash command launchers (wb-*.md)
data/                                  # Generated data (gitignored)
sidecar_jobs/                          # Scheduled job definitions
```

