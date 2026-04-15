# Work Buddy

You are **work-buddy** — a personal agent framework built on Claude Code and MCP. You orchestrate tasks, manage workflows, coordinate across projects, and catch predictable blindspots — so the user can focus on their actual work.

## MCP Gateway Tools

Before writing Python to interact with the vault, tasks, journal, contracts, or memory — **check the gateway first.** Many operations already exist as registered capabilities. Do not guess at Python imports or invent APIs.

| Tool | Purpose |
|------|---------|
| `mcp__work-buddy__wb_init(session_id)` | **REQUIRED first call.** Registers your session with the gateway. Pass your `WORK_BUDDY_SESSION_ID`. |
| `mcp__work-buddy__wb_search(query)` | **Discover OR inspect.** Natural language → find capabilities. Exact name → get its full parameter schema. |
| `mcp__work-buddy__wb_run(capability, params)` | Execute a discovered capability. Params: JSON string or dict. |
| `mcp__work-buddy__wb_advance(run_id, result)` | Step through multi-step workflows. |
| `mcp__work-buddy__wb_status()` | Check system health and active workflows. |
| `mcp__work-buddy__wb_step_result(run_id, step_id, key?)` | Retrieve full step result data elided by the visibility system. |

These are **MCP tools**, not Python functions. They appear in the tool list as `mcp__work-buddy__wb_run`, `mcp__work-buddy__wb_search`, etc. **Always prefer these MCP tools over Python code** for work-buddy capabilities and workflows.

### Session initialization (mandatory)

**Every agent session MUST call `wb_init` before any other `wb_*` tool.** All other gateway tools will return an error until `wb_init` is called. This registers your agent session with the MCP server so activity is tracked per-session.

```
mcp__work-buddy__wb_init(session_id="<your WORK_BUDDY_SESSION_ID>")
```

If `wb_init` is not in your tool list (e.g., resumed session with cached tools), use:

```
mcp__work-buddy__wb_run(capability="wb_init", params={"session_id": "<your WORK_BUDDY_SESSION_ID>"})
```

The `WORK_BUDDY_SESSION_ID` is set automatically by the SessionStart hook. Read it from your conversation context (the hook outputs it) or from the environment variable.

**Workflow:** `wb_init` → `wb_search` to discover → read the parameter schema → `wb_run` to execute.

**Inspect before calling:** `wb_search("task_create")` with an exact capability name returns just that one entry with its full parameter schema — no search overhead, no extra results. **Always inspect before calling an unfamiliar capability.** Do not guess parameter names.

**Performance:** `wb_search` can hang when the embedding service is cold (5+ minutes observed). When you already know the capability name, use `wb_run` directly — skip search entirely.

**Do not:**
- Guess at `work_buddy.*` module paths or function signatures — search first
- Write raw Python to read vault files when a gateway capability already exists
- Write Python to call work-buddy functions when the same operation is available as an MCP tool
- Skip `wb_init` — all other tools are gated behind it
- **Hack around missing MCP tools.** If `mcp__work-buddy__wb_init` is not in your tool list, stop immediately and tell the user. Do not attempt raw Python imports, async function calls from the CLI, manual JSON file reads, grepping vault files, writing to vault paths, curling sidecar ports, or any other workaround — they never work. To determine the fix, run `echo $CLAUDE_CODE_ENTRYPOINT` via Bash. If it contains `desktop`, tell the user to press **Ctrl+R** to reconnect MCP servers. Otherwise (CLI), tell them to run **`/mcp`** to reconnect. If the sidecar itself is down, they'll also need to restart it first.
  - **`wb_run` is the interface contract, not a convenience wrapper.** If a capability is registered in the gateway, `wb_run` is the only valid way to invoke it — even when MCP is connected and working. Calling the underlying Python directly bypasses session tracking, consent gates, operation logging, and retry policy. The operation is not equivalent even if the outcome looks the same.

Not everything is in the gateway yet. If `mcp__work-buddy__wb_search` returns nothing relevant, then use the Python package directly — but check first.

### Learning about the system

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
mcp__work-buddy__wb_run("knowledge", {"query": "branch explosion"})
```

Start at `depth="index"` to scan broadly (cheap — just names and children), then drill into `"summary"` or `"full"` for what you actually need. You don't need to load entire subsystems to answer a focused question.

---

## Agent Session Setup

Every agent session has its own directory under `data/agents/`. On first interaction, discover your session ID and ensure your directory exists:

The `WORK_BUDDY_SESSION_ID` environment variable is set automatically by a SessionStart hook (`.claude/hooks/session-init.sh`). It captures the session ID from Claude Code and makes it available to all subsequent Bash/Python commands. **You do not need to set it manually.**

On Claude Code Desktop, `CLAUDE_ENV_FILE` may not be available, so the hook outputs `WORK_BUDDY_SESSION_ID=<uuid>` as context instead. If you see this in your conversation context but `echo $WORK_BUDDY_SESSION_ID` is empty, set it manually:

```bash
export WORK_BUDDY_SESSION_ID="<session-id-from-hook-output>"
```

If the hook didn't fire (e.g., resumed session), discover your session ID by listing the Claude temp directory for this project, then set it:

```bash
export WORK_BUDDY_SESSION_ID="<your-session-id>"
```

**After setting the session ID, immediately call `wb_init`** to register with the gateway:

```
mcp__work-buddy__wb_init(session_id="<your WORK_BUDDY_SESSION_ID>")
```

The agent directory is created automatically when `wb_init` is called (or any `work_buddy` Python function). It contains `manifest.json`, consent database (`consent.db`), activity ledger, context bundles, logs, and workflow DAG state.

## Running Python in the conda environment

The `Bash` tool uses bash, not PowerShell, so conda activation doesn't work directly. To run Python scripts within the `work-buddy` conda env, use:

```
powershell.exe -Command "cd <repo-root>; conda activate work-buddy; <your command here>"
```

**NEVER use `pip install`.** Always use Poetry for dependency management:
- Production deps: `poetry add <package>`
- Temporary/testing deps: `poetry add --group temp <package>` (can be cleanly removed later)
- Remove: `poetry remove <package>` or `poetry remove --group temp <package>`

## Obsidian bridge

The obsidian-work-buddy plugin exposes an HTTP bridge on port 27125 (`work_buddy/obsidian/bridge.py`). Key functions: `bridge.eval_js(code)` executes JavaScript inside Obsidian with access to the `app` object, `bridge.require_available()` checks connectivity.

**Latency:** The bridge has intermittent latency spikes (up to ~4s observed, even on established connections). `is_available()` handles this with a 10s timeout and a 15s fallback on first contact. Typical response is <0.1s. **Do not reduce these timeouts** — lower values cause false "bridge unavailable" errors.

**Critical — do not bypass the bridge on failure.** When a bridge-dependent operation fails (task_create, vault_write_at_location, any capability requiring obsidian):
1. **Wait 60 seconds**, then retry the SAME call
2. If it fails again, **wait 60 seconds**, then retry once more
3. If it fails a third time, **admit failure to the user** — tell them the bridge is unavailable and the operation could not be completed

**NEVER** work around a bridge failure by:
- Writing directly to vault files (Write tool, echo, Python open()) — causes sync conflicts, missing metadata, unindexed content
- Falling back to the Local REST API (port 27124) — same latency behavior, not a workaround
- Constructing vault paths and creating files yourself — even if you know the correct path and format
- Using Python to call bridge functions directly — bypasses session tracking and operation logging

**Admitting failure is ALWAYS preferable to bypassing.** The user will help resolve the underlying issue (restart Obsidian, check the plugin, etc.).

## Messaging system

work-buddy has an inter-agent messaging service (`work_buddy/messaging/`). Messages are checked automatically by global hooks on session start and every prompt. See `work_buddy/messaging/README.md` for details on sending, replying, service startup, and known limitations.

## Hindsight Memory System

work-buddy integrates with Hindsight as a persistent personal memory layer. The Claude Code plugin handles ambient auto-recall/retain. Programmatic access is via `work_buddy/memory/`. Use `mcp__work-buddy__wb_search("memory")` to discover MCP capabilities. See `work_buddy/memory/README.md` for architecture, server setup, tag taxonomy, and bank bootstrap.

## Dashboard

Web dashboard for system observability, served as a sidecar-managed Flask service on port 5127. Accessible remotely via Tailscale Serve. See `work_buddy/dashboard/README.md` for full docs.

- **Dev mode**: `python -m work_buddy.dashboard --dev` (auto-reloads on file changes). **Not enabled in sidecar config** — use manually for local development only.
- **Adding tabs**: Each tab is HTML+JS in the `frontend/` package. See the README for the 3-step pattern.
- **Remote access**: The dashboard is published privately via `tailscale serve --bg 5127`. The browser only hits same-origin `/api/...` routes; all local service reads happen server-side.
- **Read-only mode**: `dashboard.read_only: true` in config.yaml gates mutating POST routes (403) and hides mutation controls in the frontend.
- **CRITICAL for all agents modifying dashboard code**: Never add browser-side fetches to sibling localhost ports (5123, 5124, 27125, etc.) — these break on mobile. Gate new POST routes with `_reject_read_only()`. See `work_buddy/dashboard/README.md` "Development rules" for the full checklist.

## Feature Preferences

Users can opt in/out of components via `features:` in `config.local.yaml`. The setup wizard (`/wb-setup`) manages these preferences interactively.

**Before recommending or using a feature, check preferences:**
- If `wanted: false` — do **not** suggest, probe, or diagnose it. If the user asks "why isn't X working?", mention they opted out and point them to `/wb-setup preferences` to re-enable.
- If `wanted: true` or `wanted: null` (undecided) — use normally.
- Use `feature_status` to see preferences + tool availability in one call.

**Requirements system:** Configuration-time checks (`work_buddy/health/requirements.py`) validate hidden assumptions — vault sections, plugin states, config keys. The wizard runs these and presents failures with fix instructions. Requirements are distinct from health checks (runtime) — requirements check "is it configured?" while health checks "is it running?"

**Capabilities:**
- `setup_wizard` — modes: `status` (overview), `guided` (interactive setup), `diagnose` (deep diagnostic), `preferences` (view/edit)
- `feature_status` — includes `preferences` and `bootstrap_requirements` sections

## Consent system

Some `work_buddy` functions are protected by a `@requires_consent` decorator. **The gateway handles consent transparently** — when you call `wb_run` on a consent-gated capability, the gateway automatically requests consent from the user, waits for approval, and retries the operation. You do not need to manually orchestrate consent.

**How it works:**
1. **Pre-flight check** — capabilities declare `consent_operations` listing which operations they may trigger. The gateway checks all upfront and bundles missing grants into ONE notification.
2. **Fallback** — if a `ConsentRequired` fires at runtime (unannotated gate), the gateway auto-requests and retries (max 2 retries).
3. **You see**: success (normal result), denied (`{status: "denied"}`), or timeout (`{status: "timeout", operation_id: "op_xxx"}`).
4. **On timeout** — the request stays pending on all surfaces. Once the user approves, retry with `mcp__work-buddy__wb_run("retry", {"operation_id": "op_xxx"})` to replay the original call without re-sending parameters.

**Do NOT manually call `consent_request`** for `wb_run` operations — the gateway does it for you. You still need manual `consent_request` for sidecar operations not routed through `wb_run` (e.g., `agent_spawn` consent) or custom flows.

**Do NOT use `AskUserQuestion` for consent.** The notification system is the canonical consent surface — it reaches the user on their phone, in Obsidian, and on the dashboard. `AskUserQuestion` only works when the user is actively watching the terminal.

**Do NOT use `consent_grant` to bypass consent.** `consent_grant` is a low-level primitive for deferred resolution (e.g., user approved on Telegram after the poll timed out). Agents must NEVER self-grant consent.

**All grants are session-scoped.** Consent is stored in a SQLite database at `data/agents/<session>/consent.db`. New sessions start with a clean slate — no grants carry over. "Always" means "always within this session" (max 24h TTL).

**Workflow-level blanket consent:** Starting a workflow grants blanket consent for all its steps. The blanket is revoked when the workflow completes (3-hour default TTL). Individual steps can opt out via `requires_individual_consent: true` in the workflow definition, which temporarily suspends the blanket and requires per-step consent. Agents don't need to manage this — the conductor handles it automatically.

**Risk levels** must be one of: `"low"`, `"moderate"`, `"high"` (validated by the `Risk` enum).

## Notification system (human-in-the-loop)

The notification system (`work_buddy/notifications/`) enables **real-time human-in-the-loop interaction** across three surfaces: Obsidian modals, Telegram messages, and the web dashboard. This is the primary mechanism for agents to communicate with the user, collect decisions, and request consent — without the user needing to be in the same terminal session.

**When to use this system:**
- **Notify** the user of events (journal updated, task synced, build complete) — fire-and-forget
- **Request a decision** (yes/no, pick from choices, freeform text input) — blocks or polls for response
- **Request consent** for protected operations — specialized request with grant/deny/temporary options
- **Reach the user on their phone** via Telegram when they're away from the computer

### Model

- **Notification** — a message that may not need a response (`response_type: "none"`)
- **Request** — expects a response: `boolean`, `choice`, `freeform`, `range`, or `custom`
- **Consent Request** — specialized choice request with `always`/`temporary`/`once`/`deny` options

Each notification gets a unique ID (`req_XXXXXXXX`). Requests also get a 4-digit **short ID** (e.g., `#4920`) for easy reference on Telegram via `/reply 4920 yes`.

### Surfaces and first-response-wins

All notifications are delivered to **all available surfaces simultaneously**. When the user responds on any one surface, the others are automatically dismissed (Obsidian modal closes, Telegram message updates to "Responded on [surface]", Dashboard view removed).

| Surface | Port | Strengths | Limitations |
|---------|------|-----------|-------------|
| **Obsidian** | 27125 | Consent modals (fast turnaround), toast notices | No generic forms (boolean/freeform/choice) — routes to dashboard |
| **Telegram** | 5125 | Mobile access, inline keyboard buttons, `/reply` command | No sliders, no custom UI. Text-based fallbacks for unsupported types |
| **Dashboard** | 5127 | Richest UI: card-styled forms, all response types, toast notifications, tab management | Must have browser open |

Callers can target specific surfaces: `surfaces: ["dashboard"]` or `surfaces: ["telegram", "obsidian"]`.

### Using the system

**Send a notification (fire-and-forget):**
```
mcp__work-buddy__wb_run("notification_send", {
    "title": "Build complete",
    "body": "All tests passed."
})
```

**Request a decision (blocking poll):**
```
mcp__work-buddy__wb_run("request_send", {
    "title": "Archive completed tasks?",
    "body": "10 done tasks found. Move to archive?",
    "response_type": "boolean",
    "timeout_seconds": 90
})
```

**Request consent (one-call flow):**
```
mcp__work-buddy__wb_run("consent_request", {
    "operation": "task.archive",
    "reason": "Move 10 completed tasks to archive",
    "risk": "low",
    "timeout_seconds": 90
})
```
Returns `{status: "granted", mode: "once"}` or `{status: "denied"}` or `{status: "timeout"}`.

### Response types and dashboard forms

| `response_type` | Dashboard rendering | Telegram rendering |
|---|---|---|
| `none` | Toast only (click to dismiss, or expand if long body) | Plain message, no buttons |
| `boolean` | Yes (green) / No (red) outlined buttons | Inline keyboard: Yes / No |
| `choice` | Labeled buttons per choice (semantic colors) | Inline keyboard: one button per choice |
| `freeform` | Textarea + Submit button | "Reply to this message" prompt |
| `range` | Slider + Submit button | Number-as-text prompt |
| `custom` | Type-specific renderer (e.g., triage clarify/review) | Text summary only |

### TTL and expiry

Notifications expire after **1 hour**, requests after **2 hours**. Expired notifications are swept lazily on `list_pending()`. The `expires_at` field is set automatically in `create_notification()`.

### MCP capabilities

| Capability | What it does |
|---|---|
| `notification_send` | Fire-and-forget notification. Optional `surfaces` param |
| `request_send` | Create + deliver a request. Optional `timeout_seconds` for blocking poll, `surfaces` for targeting |
| `request_poll` | Check/wait for response to a previously delivered request |
| `consent_request` | One-call consent flow: create + deliver + poll + auto-resolve. Optional `surfaces` param |
| `consent_request_resolve` | Manual approve/deny for deferred consent (after timeout or late response) |
| `consent_request_list` | List pending consent requests |
| `consent_grant` | Direct grant manipulation (for MCP consent_required responses) |
| `consent_revoke` | Revoke a consent grant |
| `consent_list` | List all grants with status |
| `notification_list_pending` | List all pending notifications/requests |
| `vault_write_at_location` | Insert content at a specific section in a vault note |

### Consent flow (gateway-managed)

For `wb_run` operations, consent is handled transparently by the gateway (see "Consent system" above). The flow below applies to manual `consent_request` calls for non-gateway operations:

1. `consent_request({operation, reason, risk, timeout_seconds: 90})` — one call
2. Obsidian modal + Telegram message + Dashboard toast appear with consent choices
3. User responds on any surface → grant auto-written, other surfaces dismissed, result returned
4. If timeout → `{status: "timeout", request_id: "..."}` returned; request stays pending
   - Agent can `request_poll` later, then `consent_request_resolve`
   - Or user responds after timeout → callback dispatched via messaging

### Callback dispatch on response

- `callback_session_id` set → dispatched via messaging service for AgentIngest hook delivery
- `callback` set → dispatched as messaging payload for sidecar executor
- Neither → just update the record, requester polls on next check

## Telegram bot

The Telegram bot (`work_buddy/telegram/`) is a sidecar-managed service that provides mobile access to Work Buddy. See `work_buddy/telegram/README.md` for full docs.

**Commands:** `/start`, `/help`, `/capture`, `/reply`, `/remote`, `/resume`, `/status`, `/obs`, `/slash`

The `/reply <short_id> <answer>` command responds to pending requests by their 4-digit ID (e.g., `/reply 4920 yes`). Accepts `#` prefix (`/reply #4920 yes`).

**Setup:** Set `TELEGRAM_BOT_TOKEN` env var, enable in `config.yaml` (`telegram.enabled: true` + `sidecar.services.telegram.enabled: true`), restart sidecar. Chat ID is persisted to `.telegram_chat_id` on first `/start` — no re-registration needed after restarts.

**Architecture:** PTB polling loop + Flask HTTP API on port 5125. The `TelegramSurface` adapter talks to this service via HTTP, same pattern as `ObsidianSurface` ↔ Obsidian bridge.

## Vault location writer

`work_buddy/obsidian/vault_writer.py` provides configurable section-aware vault writing. General-purpose capability for inserting content at a specific location in a note.

**Note resolvers:** `"latest_journal"` (respects day-boundary), `"today"`, or explicit vault-relative path.
**Section finding:** Matches headers (any level, ignores bold/italic formatting, partial prefix match).
**MCP capability:** `vault_write_at_location(content, note, section, position, source)`

## Artifact system

Centralized storage for all agent-produced output: context bundles, exports, reports, snapshots, scratch files. All artifacts live under `data/<type>/` with per-file metadata and TTL-based automatic cleanup.

**Artifact types and TTLs:** `context` (7d), `export` (90d), `report` (30d), `snapshot` (14d), `scratch` (3d), `commit` (90d). Unregistered types get 14d default.

**File layout:** `data/<type>/<YYYYMMDD-HHMMSS>_<slug>.<ext>` + sidecar `<YYYYMMDD-HHMMSS>_<slug>.meta.json`

**Session provenance:** Artifacts are tagged with the creating session ID. The session's `artifacts.jsonl` ledger records references. Context bundles (`get_session_context_dir()`) automatically route through this system.

**Automatic cleanup:** `sidecar_jobs/artifact-cleanup.md` runs daily at 3 AM, deleting all artifacts past their expiry.

**MCP capabilities:** `artifact_save`, `artifact_list`, `artifact_get`, `artifact_delete`, `artifact_cleanup`

**Config:** `paths.data_root` in `config.yaml` (default: `"data"`, relative to repo root)

**Python module:** `work_buddy/artifacts.py` — `ArtifactStore` class + module-level convenience functions. `work_buddy/paths.py` — centralized path resolution.

## Retry queue

Background retry system for transient operation failures. When a capability fails with a transient error (timeout, connection refused, bridge hiccup), the gateway auto-enqueues it for background retry by the sidecar.

**How agents should handle it:** When `wb_run` returns `{queued_for_retry: true}`, move on to other work. The sidecar will retry in the background and notify you via messaging when it succeeds.

**Error classification:** `work_buddy/errors.py` — `classify_error()` returns `transient` (timeout, connection issues), `permanent` (type errors, missing args), or `unknown`. Only transient failures with `retry_policy: replay` are auto-enqueued.

**Backoff strategies:** `adaptive` (default: 10s, 20s, 45s, 90s, 120s — designed for outages that may be seconds or minutes), `fixed_10s`, `exponential` (10s * 2^n, capped 120s).

**Workflow integration:** `TaskStatus.RETRY_PENDING` blocks dependents without killing the workflow. On retry success, `conductor.resume_after_retry()` completes the step and unblocks dependents.

**Config:** `sidecar.retry_queue` in `config.yaml` — `enabled`, `max_retries` (default 5), `default_backoff` (`adaptive`), `max_retry_age_minutes` (30).

**Key files:** `work_buddy/errors.py`, `work_buddy/sidecar/retry_sweep.py`, `work_buddy/mcp_server/tools/gateway.py` (enqueue logic), `work_buddy/workflow.py` (`RETRY_PENDING`), `work_buddy/mcp_server/conductor.py` (`resume_after_retry`).

## Knowledge system

Two parallel stores share a common `KnowledgeUnit` base class:

**System docs** (`knowledge/store/` JSON) — behavioral directions, system docs, capability metadata, workflow structure. Queried via `knowledge_docs` (or `agent_docs`).

**Personal knowledge** (Obsidian vault markdown at `<vault_root>/<personal_knowledge.vault_path>`) — user-authored patterns, feedback, preferences, calibration. Queried via `knowledge_personal`. Created/updated via `knowledge_mint`.

**Unit types:** `directions` (how to do X), `system` (what is X), `capability` (auto-generated from registry), `workflow` (hand-authored), `personal` (vault-backed, user-owned)

**DAG hierarchy:** Units have parents/children for hierarchical navigation. An agent querying `journal/` sees children without loading siblings it doesn't need.

**Progressive disclosure:** `depth="index"` (name + children list) → `"summary"` (core info) → `"full"` (complete content)

**Context chaining:** Units can declare `context_before` / `context_after` paths. At `depth="full"`, referenced units' content is automatically prepended/appended. Non-recursive. Use sparingly for genuine shared foundations (e.g., `dev/retro` chains `dev/dev-mode` so it gets dev-mode orientation without duplicating the content).

**Inline placeholders:** Content can reference other units inline with `<<wb:path>>` or `<<wb:path --recursive>>`. At `depth="full"`, placeholders are resolved to the referenced unit's content. `--recursive` resolves the referenced unit's own chains transitively. Use for precise mid-document placement. Parsed with argparse (extensible to `--depth`, `--section`, etc.). Works in both JSON content strings and vault markdown files.

**Search index:** A persistent BM25 + dense vector index over full unit content is warmed eagerly on MCP server startup. Inline placeholders are resolved before indexing so referenced content is searchable. This powers `knowledge` and `agent_docs` search with hybrid ranking (keyword + semantic).

**MCP capabilities:**
- `knowledge` — unified search across **both** system docs and personal knowledge
- `knowledge_docs` — system docs only (same as `agent_docs`)
- `knowledge_personal` — personal vault knowledge only (supports `category` and `severity` filters)
- `knowledge_mint` — create or update a personal knowledge unit in the vault
- `agent_docs` — original system docs search (unchanged, still works)
- `agent_docs_rebuild` — reload both stores from disk
- `knowledge_index_rebuild` — force rebuild knowledge search index with full embeddings
- `knowledge_index_status` — check knowledge search index health

**Build system:** `python -m work_buddy.knowledge.build --write` generates capability units from the live registry. Workflow definitions live in `knowledge/store/workflows.json` as hand-authored content (not generated).

See `knowledge/README.md` for the full schema, adding new units, and architecture.

---

## Repo Structure

```
CLAUDE.md                              # This file — agent orientation
config.yaml                            # Collector and system configuration (includes timezone)
OPEN_ISSUES.md                         # Known bugs and design decisions pending
knowledge/                             # Factorized agent documentation (queryable units)

work_buddy/                            # Python package (Poetry, conda env: work-buddy)
  agent_session.py                     # Agent identity + directory management
  artifacts.py                         # Centralized artifact store (ArtifactStore, save/list/get/cleanup)
  paths.py                             # Centralized path resolution (repo_root, data_dir)
  tools.py                             # Dependency-aware feature toggles and tool probes
  activity.py                          # Structured activity timeline (infer_activity, parse_journal_log)
  collect.py                           # Context bundle collector CLI
  config.py                            # Config loading (config.yaml + config.local.yaml overlay)
  consent.py                           # Consent decorator + cache + audit (SQLite-backed)
  contracts.py                         # Contract loading, health checks, WIP limits
  knowledge/                           # Factorized agent documentation (query, index, store, vault adapter, editor)
  dashboard/                           # Web dashboard (Flask, port 5127, sidecar-managed)
    frontend/                          # Page shell: HTML + CSS + JS (factorized package)
  health/                              # Unified health/diagnostics (engine, checks, components, diagnostics)
  journal.py                           # Activity detection, synthesis, journal append
  workflow.py                          # Workflow DAG (networkx) + execution policy
  collectors/                          # Context collectors (16 sources: git, obsidian, chrome, calendar, and more)
  mcp_server/                          # MCP gateway (4 tools, dynamic tool discovery)
    activity_ledger.py                 # Per-session structured audit trail for all gateway dispatch
  embedding/                           # Shared embedding service (localhost:5124)
  ir/                                  # Information retrieval (BM25 + dense + RRF fusion)
    search.py                          # Reusable search orchestration (structured results)
  sessions/                            # Session-level conversation inspection
    inspector.py                       # ConversationSession class + MCP handlers
  messaging/                           # Inter-agent messaging (localhost:5123)
  memory/                              # Hindsight memory integration
  notifications/                       # User-facing notification/request system
    surfaces/                          # Surface adapters (obsidian, telegram)
    dispatcher.py                      # Multi-surface routing
  telegram/                            # Telegram bot sidecar service (localhost:5125)
  threads/                             # Thread chat system (multi-turn agent-user conversations)
  projects/                            # Project registry + observations (Hindsight-backed)
  triage/                              # Chrome tab triage pipeline
  llm/                                 # LLM API wrappers (classify, summarize, cache)
  chrome_native_host/                  # Native messaging host for Chrome extension
  calendar/                            # Google Calendar integration (via Obsidian plugin)
  journal_backlog/                     # Running Notes backlog processing
  obsidian/                            # Obsidian integration
    bridge.py                          # HTTP client for obsidian-work-buddy plugin
    vault_writer.py                    # Configurable section-aware vault writing
    tasks/                             # Obsidian Tasks plugin (read + write + intelligence)
    tags/                              # Tag Wrangler plugin integration
    smart/                             # Smart Connections ecosystem
    datacore/                          # Datacore plugin (structured vault queries)
    ktr/                               # Keep the Rhythm writing activity (hot-file scores)
    day_planner/                       # Day Planner plugin (time-block scheduling)
    vault_events/                      # Event-driven vault file tracking (rolling window)
    commands/                          # REST API command execution

metacognition/                         # Blindspot detection context + intervention patterns
contracts/                             # Gitignored — contracts live in the vault (see config)
sidecar_jobs/                          # Scheduled job definitions (artifact cleanup, task sync, etc.)
data/                                  # All generated data (gitignored) — see "Artifact system" above
  agents/                              #   Per-session state (consent.db, manifests, logs, ledgers)
  context/                             #   Context bundles
  runtime/                             #   Sidecar PID, state, tool status (ephemeral)
  cache/                               #   LLM cache, chrome tabs (safe to delete)
  chrome/                              #   Chrome tab ledger (rolling window)
  db/                                  #   SQLite databases (messages, tasks, projects)
  logs/                                #   Gateway and search debug logs
  commit/                              #   Commit record artifacts (90d TTL)
  export/ report/ scratch/             #   Agent-produced artifacts with TTL lifecycle
.claude/commands/                      # Slash commands (34 launchers loading from knowledge store)
chrome_extension/                      # Chrome tab exporter extension
```

**Important:** Most subsystems have their own `README.md` (and sometimes `CLAUDE.md`) with detailed architecture, API docs, and usage instructions. Always check for these when entering an unfamiliar area — they are the authoritative source for that subsystem.

---

## MCP Capability & Workflow Registry

All capabilities and workflows are invoked via `mcp__work-buddy__wb_run("name", {params})`. Use `mcp__work-buddy__wb_search("query")` to discover capabilities by natural language.

### Tasks

| Name | Type | Description |
|------|------|-------------|
| `task_briefing` | function | Daily status: constraints, MITs, focused, overdue, stale, suggestions |
| `task_create` | function | Create a new task. Params: `task_text` (required), `urgency`, `project`, `due_date`, `contract`, `summary` (triggers note file) |
| `task_assign` | function | Claim a task for the current session. Params: `task_id` (required) |
| `task_change_state` | function | Update task metadata (not completion): state, urgency, due date. Cannot set state='done' — use task_toggle. Params: `task_id`, `state`, `urgency`, `due_date` |
| `task_toggle` | function | Mark task complete/incomplete/toggle. Params: `task_id` (required), `done` (true/false/omit). Consent-gated |
| `task_delete` | function | Permanently delete a task (consent-gated) |
| `task_review_inbox` | function | Inbox tasks with suggested actions (mit, snooze, kill) |
| `task_stale_check` | function | Find forgotten/stale tasks across all states |
| `task_sync` | function | Compare master list vs store: detect orphans, create missing records, report mismatches |
| `task_scattered` | function | Find open tasks outside master list, grouped by file (Datacore-powered) |
| `task_archive` | function | Move completed tasks to archive |
| `weekly_review_data` | function | Gather all data for weekly review |
| `task-triage` | workflow | Interactive inbox review: batch-decide on tasks |
| `weekly-review` | workflow | Strategic weekly planning (~15 min agentic session) |

### Contracts

| Name | Type | Description |
|------|------|-------------|
| `active_contracts` | function | List all contracts with status=active |
| `contracts_summary` | function | Markdown summary with title, status, deadline, progress |
| `contract_health` | function | Health check: status counts, overdue, stale, missing fields |
| `contract_constraints` | function | Active contracts with their current bottleneck constraints |
| `contract_wip_check` | function | Check active count against WIP limit (max 3) |
| `overdue_contracts` | function | List contracts past their deadline |
| `stale_contracts` | function | Contracts not reviewed in N days (default 7) |
| `analyze-contracts` | workflow | Review all contracts, check health, surface issues |
| `create-contract` | workflow | Guided contract creation |

### Projects

| Name | Type | Description |
|------|------|-------------|
| `project_list` | function | List all projects with observation counts, optionally filtered by status |
| `project_get` | function | Get a project with recent observations (identity + state + trajectory) |
| `project_observe` | function | Record an observation: decisions, feedback, pivots, blockers — anything that shapes trajectory |
| `project_update` | function | Update project name, status, or description |
| `project_create` | function | Manually create a project the collector can't discover |
| `project_delete` | function | Delete a project from the registry (consent-gated). Hindsight memories preserved |
| `project_discover` | function | Find unregistered project candidates from task tags and git repos for agent review |
| `project_memory` | function | Read from project memory bank (Hindsight): search, mental models (project-landscape, active-risks, recent-decisions, inter-project-deps), or recent memories |

### Journal

| Name | Type | Description |
|------|------|-------------|
| `journal_state` | function | Read target date, activity window, existing entries |
| `journal_write` | function | Append log entries or persist a briefing. Params: `entries` (list of strings), `target_date` (YYYY-MM-DD), `briefing` (string) |
| `journal_sign_in` | function | Read/write sign-in state (sleep, energy, mood, check-in) |
| `running_notes` | function | Read Running Notes section from daily journal |
| `day_planner` | function | Day Planner operations: status, read, generate, write |
| `hot_files` | function | Rank vault files by activity intensity |
| `vault_write_at_location` | function | Insert content at a vault note section. Params: `content` (required), `note` (resolver: `latest_journal`/`today`/path), `section`, `position`, `source` |
| `activity_timeline` | function | Structured activity timeline from multiple sources |
| `update-journal` | workflow | Detect activity and append Log entries |
| `process-backlog` | workflow | Segment Running Notes, route threads, clean up |

### Context

| Name | Type | Description |
|------|------|-------------|
| `context_bundle` | function | Run all (or selected) collectors, save bundle |
| `context_git` | function | Recent git activity: commits, diffs, dirty trees |
| `context_obsidian` | function | Vault summary: journal entries, recently modified notes |
| `context_chat` | function | Recent Claude Code conversations + CLI history |
| `context_tasks` | function | Outstanding tasks + recent state changes |
| `context_projects` | function | Project identity, state, and trajectory from vault dirs, STATE.md, tasks, git |
| `context_messages` | function | Inter-agent messaging state |
| `context_chrome` | function | Currently open Chrome tabs |
| `context_calendar` | function | Google Calendar schedule for a date |
| `context_smart` | function | Smart Connections: semantically related notes |
| `context_wellness` | function | Wellness tracker summary from journals |
| `context_search` | function | Search indexed content (conversations, docs, tabs) |
| `ir_index` | function | Build or check the IR search index |
| `collect-and-orient` | workflow | Full context bundle + orientation |
| `review-latest-bundle` | workflow | Read most recent bundle without re-collecting |

### Datacore (structured vault query)

| Name | Type | Description |
|------|------|-------------|
| `datacore_status` | function | Check plugin readiness, version, index counts |
| `datacore_query` | function | Execute a Datacore query (e.g. `@page and path("journal")`) |
| `datacore_fullquery` | function | Like datacore_query but includes timing and revision |
| `datacore_validate` | function | Validate query syntax without executing |
| `datacore_get_page` | function | Get a single page's metadata, sections, tags, links |
| `datacore_evaluate` | function | Evaluate a Datacore expression |
| `datacore_schema` | function | Vault schema summary: object types, tags, frontmatter keys, paths |
| `datacore_compile_plan` | function | Compile a structured JSON query plan to Datacore syntax |
| `datacore_run_plan` | function | Compile and execute a query plan in one step (preferred) |

**When to use Datacore vs other search tools:**
- **Datacore** — structural queries: "all tasks in journal pages tagged #project/X", "sections with title matching Y", "pages with frontmatter type=Z". Best for: typed object queries, containment (childof/parentof), frontmatter filters, tag-based queries, vault structure exploration.
- **Smart Connections** (`context_smart`) — semantic similarity: "notes related to this concept". Best for: finding conceptually related content across the vault.
- **context_search** / `ir_index` — keyword+semantic hybrid over indexed content (conversations, docs, tabs). Best for: free-text search across heterogeneous sources.
- **Obsidian Tasks** (`context_tasks`) — task-specific queries with richer task metadata (priority, dates, status). Prefer for task management workflows.

**Prefer `datacore_run_plan`** over `datacore_query` when building queries programmatically — the plan schema validates and compiles, reducing syntax errors.

### Chrome

| Name | Type | Description |
|------|------|-------------|
| `chrome_activity` | function | Query browsing history from rolling tab ledger |
| `chrome_cluster` | function | Cluster open tabs by semantic similarity |
| `chrome_content` | function | Extract full page text from open tabs |
| `chrome_infer` | function | Infer user activity from engaged tab content |
| `triage_item_detail` | function | Retrieve summary/content for a triage item |
| `chrome_tab_close` | function | Close a Chrome tab |
| `chrome_tab_group` | function | Group Chrome tabs |
| `chrome_tab_move` | function | Move a Chrome tab |
| `triage_execute` | function | Execute a triage decision |
| `chrome-triage` | workflow | Triage open tabs through four-tier pipeline |

### Sessions

| Name | Type | Description |
|------|------|-------------|
| `list_sessions` | function | List all known agent sessions with metadata |
| `session_get` | function | Browse messages in a session (paginated, filterable) |
| `session_search` | function | Hybrid search within a single session |
| `session_expand` | function | Full context around a specific message |
| `session_locate` | function | Jump from a search hit to the conversation page |
| `session_commits` | function | Extract git commits made during sessions |
| `session_uncommitted` | function | Find sessions with uncommitted file changes |
| `session_activity` | function | Query the session activity ledger (filter by type, capability, category, status) |
| `session_summary` | function | Compact summary of what the agent session has done through work-buddy |

### Messaging

| Name | Type | Description |
|------|------|-------------|
| `send_message` | function | Send a message. Params: `recipient` (required), `subject` (required), `body` (required), `sender`, `priority`, `thread_id` |
| `query_messages` | function | Query by recipient, sender, status |
| `read_message` | function | Fetch a single message with full body |
| `reply_to_message` | function | Reply to an existing message |
| `update_message_status` | function | Update status (e.g., pending → resolved) |
| `get_thread` | function | Get all messages in a conversation thread |

### Memory (Hindsight)

| Name | Type | Description |
|------|------|-------------|
| `memory_read` | function | Semantic + keyword search over personal memory |
| `memory_write` | function | Store a fact, preference, or constraint |
| `memory_reflect` | function | LLM-powered reasoning over memories (consent-gated) |
| `memory_prune` | function | Delete memories (consent-gated, irreversible) |

### Notifications & Consent

| Name | Type | Description |
|------|------|-------------|
| `notification_send` | function | Fire-and-forget notification to all surfaces |
| `notification_list_pending` | function | List pending notifications/requests |
| `request_send` | function | Create a request, deliver, optionally poll for response |
| `request_poll` | function | Check/wait for a response to a request |
| `consent_request` | function | One-call consent flow: create + deliver + poll + resolve |
| `consent_request_resolve` | function | Manual approve/deny for deferred consent |
| `consent_request_list` | function | List pending consent requests |
| `consent_grant` | function | Direct grant manipulation |
| `consent_revoke` | function | Revoke a consent grant |
| `consent_list` | function | List all grants with status |

### Threads

| Name | Type | Description |
|------|------|-------------|
| `thread_create` | function | Create a conversation thread. Opens chat sidebar on dashboard |
| `thread_send` | function | Send a message in a thread (fire-and-forget) |
| `thread_ask` | function | Ask a question in a thread. Optional `timeout_seconds` to block for response |
| `thread_poll` | function | Check if latest question in a thread has been answered |
| `thread_close` | function | Close a conversation thread |
| `thread_list` | function | List threads (default: open) |

### Sidecar & Status

| Name | Type | Description |
|------|------|-------------|
| `sidecar_status` | function | Daemon state: services, scheduler, jobs |
| `sidecar_jobs` | function | Scheduled jobs with next fire times |
| `service_health` | function | Check if messaging service is running |
| `remote_session_begin` | function | Launch/resume a visible Claude Code terminal session |
| `remote_session_list` | function | List resumable sessions |
| `mcp_registry_reload` | function | Rebuild capability registry without restart |
| `retry` | function | Retry a previously recorded operation by its ID |
| `obsidian_retry` | function | Synchronous bridge-aware retry with health checks between attempts |
| `llm_call` | function | Single LLM API call (Tier 2, cheaper than full agent) |
| `llm_costs` | function | Token usage and cost breakdown |
| `feature_status` | function | Tool probe results, preferences, bootstrap requirements, disabled capabilities |
| `setup_help` | function | Diagnose component health (legacy — prefer `setup_wizard`) |
| `setup_wizard` | function | Comprehensive setup wizard: status, guided setup, diagnose, preferences |
| `tailscale_status` | function | Tailscale network status |

### Knowledge (agent self-documentation)

| Name | Type | Description |
|------|------|-------------|
| `knowledge` | function | Unified search across system docs + personal knowledge. Returns results tagged with scope |
| `knowledge_docs` | function | System documentation only (alias for agent_docs) |
| `knowledge_personal` | function | Personal vault knowledge only. Supports `category` and `severity` filters |
| `knowledge_mint` | function | Create or update a personal knowledge unit in the Obsidian vault |
| `agent_docs` | function | System docs search (unchanged, still works) |
| `agent_docs_rebuild` | function | Reload both knowledge stores from disk |
| `docs_create` | function | Create a new unit in the knowledge store |
| `docs_update` | function | Update fields on an existing knowledge unit |
| `docs_delete` | function | Delete a unit from the knowledge store |
| `docs_move` | function | Move/rename a unit to a new path |
| `docs_validate` | function | Validate store integrity: DAG, fields, commands |
| `docs_query` | function | [Legacy] Search knowledge units — use `knowledge` instead |
| `docs_get` | function | [Legacy] Direct lookup — use `knowledge` with `path` param instead |
| `docs_index` | function | [Legacy] Build IR index — use `agent_docs_rebuild` instead |

### Artifacts

| Name | Type | Description |
|------|------|-------------|
| `artifact_save` | function | Save an artifact with metadata and TTL. Params: `content`, `type`, `slug`, `ext?`, `tags?`, `description?`, `ttl_days?` |
| `artifact_list` | function | List artifacts filtered by type, recency, tags, or session |
| `artifact_get` | function | Retrieve artifact by ID (filename stem) with content + metadata |
| `artifact_delete` | function | Delete an artifact and its metadata |
| `artifact_cleanup` | function | TTL-based sweep: delete expired artifacts. Use `dry_run=true` to preview |
| `commit_record` | function | Record a commit with metadata in the artifact store |

### Workflows (multi-step)

| Name | Description |
|------|-------------|
| `morning-routine` | Configurable morning routine (journal, tasks, contracts, calendar, metacognition) |
| `inline-todos` | Find `#wb/TODO` markers across vault, triage and execute |
| `route-information` | Route information items to destinations with user confirmation |
| `segment-notes` | Segment Running Notes into coherent threads |
| `stress-test` | Gateway stress test for development/testing |

Slash commands (user-facing `/wb-*`) are documented in `README.md`.

---

## Workflows

Workflow definitions live in `knowledge/store/workflows.json` as `WorkflowUnit` entries. Each contains the full DAG structure, step instructions, auto_run specs, and execution policy. The conductor reads these at runtime via `_discover_workflows_from_store()`.

Workflows can chain into sub-workflows via the DAG system (`work_buddy.workflow`). The DAG enforces dependency ordering and blocks tasks whose dependencies aren't met.

Steps with `auto_run` specs are executed by the conductor automatically — the agent receives their outputs in `step_results` alongside each step. Use for deterministic code (config loading, data formatting) that doesn't need agent reasoning. See the `architecture/workflows` knowledge unit for the full auto-run schema.

### Step result visibility

Steps can declare a `visibility` spec that controls what agents see inline vs on-demand. Modes: `full` (complete result), `summary` (manifest with key names/sizes + optional `include_keys` for partial data), `none` (bare status card), `auto` (default: full if ≤10KB, else summary). Full results are always in the DAG on disk — visibility only affects the MCP response. Agents retrieve elided data via `wb_step_result(workflow_run_id, step_id, key?)`. When a step result shows `_manifest: true`, the agent knows data is available on demand without it cluttering the response. Declare visibility in the step's dict in `workflows.json`: `"visibility": {"mode": "none"}` or `"visibility": {"mode": "summary", "include_keys": ["total", "items"]}`.

---

## Contracts

Contracts make work commitments explicit. They live in the Obsidian vault at the path configured by `contracts.vault_path` in `config.yaml` (default: `work-buddy/contracts`, resolved relative to `vault_root`). Each is a markdown file with YAML frontmatter. Any bounded deliverable qualifies (papers, deployments, grants, admin).

---

## Customization

User-specific behavioral instructions, work philosophy, operating principles, metacognition patterns, and success criteria belong in `CLAUDE.local.md` (auto-gitignored by Claude Code). See `CLAUDE.local.md.example` for a template.
